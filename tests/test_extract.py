"""L3 抽取层测试。

重点不在「代码能跑」，而在两件容易悄悄失效的事：

1. **降级不能变成静默丢数据** —— 残缺输入必须回退到 not_reported 并说清哪里被改了
2. **提示词不能漂移** —— extract.py 的规则常量与 SKILL.md 的副本必须还在说同一件事，
   防幻觉的那几句措辞（禁止脑补 n / 反例 / 自检清单）尤其不能被顺手删掉

运行::

    python -m pytest tests/test_extract.py -q
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from pfn.extract import (  # noqa: E402
    EXTRACTION_RULES,
    FEW_SHOT,
    SELF_CHECK,
    build_extraction_tasks,
    extract_via_api,
    generate_schema,
    merge_records,
    record_slug,
    validate_record,
)
from pfn.models import NOT_REPORTED, ContextPack, Confidence, Paragraph  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample_bundle.json"


# ── 公共 fixture ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def bundle() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def fig1(bundle) -> dict:
    """sample_bundle 的 Figure 1：抽取质量的黄金标准。"""
    return copy.deepcopy(bundle["figures"][0])


@pytest.fixture
def prepped(tmp_path: Path) -> Path:
    """合成一个 prep 产物目录（L1/L2 还在并行开发，不依赖它们的真实输出）。"""
    out = tmp_path / "notes_demo"
    (out / "figures").mkdir(parents=True)
    packs = [
        ContextPack(
            figure_id="Figure 1",
            kind="figure",
            caption="Fig. 1. DMC-GF exhibits pronounced antitumour activity in vitro. "
            "(A) CCK-8 assay of cell viability in U251 and U87 cells. "
            "(B) Flow cytometry of apoptosis. (C) Quantification of apoptotic rate.",
            images=["figures/fig_01.png"],
            pages=[4],
            citing_paragraphs=[
                Paragraph(
                    id="§2.3¶1",
                    section_id="§2.3",
                    page=4,
                    text="U251 and U87 cells were treated with a gradient of DMC-GF "
                    "and viability was measured by CCK-8 assay.",
                )
            ],
            methods_paragraphs=[
                Paragraph(
                    id="§4.2",
                    section_id="§4.2",
                    page=11,
                    text="Cell viability was assessed using the CCK-8 kit.",
                )
            ],
            panel_captions={"A": "CCK-8 assay", "B": "Flow cytometry", "C": "Quantification"},
            bbox_confidence=Confidence.HIGH,
        ),
        ContextPack(
            figure_id="Table 3",
            kind="table",
            caption="Table 3. Ablation on the retrieval module.",
            images=["figures/tab_03.png"],
            pages=[7],
            table_text_grid=[["Model", "EM"], ["full", "62.1"], ["w/o retrieval", "54.8"]],
            bbox_confidence=Confidence.LOW,
        ),
    ]
    (out / "context.json").write_text(
        json.dumps([p.model_dump(mode="json") for p in packs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "parsed.json").write_text(
        json.dumps({"pdf_path": "D:/papers/demo.pdf", "title": "A Demo Paper"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return out


def _fixed(warns: list[str]) -> list[str]:
    return [w for w in warns if w.startswith("[修复]")]


def _review(warns: list[str]) -> list[str]:
    return [w for w in warns if w.startswith("[复核]")]


def _fatal(warns: list[str]) -> list[str]:
    return [w for w in warns if w.startswith("[致命]")]


# ── schema ──────────────────────────────────────────────────────────────────


def test_schema_is_valid_draft_2020_12(tmp_path: Path):
    schema = generate_schema(tmp_path / "figure.schema.json")
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)  # schema 本身合法
    assert (tmp_path / "figure.schema.json").is_file()


def test_shipped_schema_matches_models():
    """契约改了却忘了重生成 schema —— 这个测试会抓到。"""
    on_disk = json.loads((ROOT / "schemas" / "figure.schema.json").read_text(encoding="utf-8"))
    assert on_disk == generate_schema(write=False), (
        "schemas/figure.schema.json 与 models.FigureRecord 不一致，"
        "请跑 `python scripts/pfn/extract.py schema` 重新生成"
    )


def test_sample_records_pass_schema(bundle):
    schema = json.loads((ROOT / "schemas" / "figure.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    for rec in bundle["figures"]:
        errors = sorted(validator.iter_errors(rec), key=str)
        assert not errors, f"{rec['figure_id']}: {[e.message for e in errors]}"


# ── validate_record：正常路径 ───────────────────────────────────────────────


def test_gold_records_validate_without_repair(bundle):
    """三条黄金样本都不该触发任何 [修复]，否则说明校验器与标准打架。"""
    for raw in bundle["figures"]:
        rec, warns = validate_record(raw)
        assert rec is not None
        assert not _fixed(warns), f"{raw['figure_id']} 不该被修复：{_fixed(warns)}"
        assert not _fatal(warns)


def test_gold_record_survives_roundtrip(fig1):
    """降级路径不能顺手改动本来就合法的数据。"""
    rec, _ = validate_record(fig1)
    assert rec is not None
    assert rec.model_dump(mode="json") == fig1


def test_schematic_with_empty_panels_is_clean(bundle):
    rec, warns = validate_record(bundle["figures"][2])  # Figure 6，示意图
    assert rec is not None and rec.figure_kind == "schematic" and rec.panels == []
    assert not _review(warns)


# ── validate_record：残缺输入的降级 ─────────────────────────────────────────


def test_missing_figure_id_is_fatal(fig1):
    fig1.pop("figure_id")
    rec, warns = validate_record(fig1)
    assert rec is None
    assert _fatal(warns) and "figure_id" in warns[0]


def test_blank_figure_id_is_fatal(fig1):
    fig1["figure_id"] = "   "
    rec, _ = validate_record(fig1)
    assert rec is None


def test_omitted_optional_field_becomes_not_reported(fig1):
    """契约里带默认值的字段漏填 = 直接得到 not_reported，这是设计好的降级，不必报警。"""
    del fig1["panels"][0]["groups"][0]["readout"]
    del fig1["panels"][0]["stats"]
    rec, warns = validate_record(fig1)
    assert rec is not None
    assert rec.panels[0].groups[0].readout == NOT_REPORTED
    assert rec.panels[0].stats.test == NOT_REPORTED
    assert not _fixed(warns), "可选字段漏填不该刷屏；真正该报的是必填字段"


@pytest.mark.parametrize(
    "path,field,expected",
    [
        (("panels", 0), "label", "-"),
        (("panels", 0, "groups", 0), "name", NOT_REPORTED),
        (("panels", 0, "groups", 0), "role", "reference"),
    ],
)
def test_missing_required_field_falls_back_with_warning(fig1, path, field, expected):
    """无默认值的字段（label / name / role）缺失才是真问题，必须降级 + 报警。"""
    node = fig1
    for part in path:
        node = node[part]
    del node[field]

    rec, warns = validate_record(fig1)
    assert rec is not None

    target = rec
    for part in path:
        target = getattr(target, part) if isinstance(part, str) else target[part]
    assert getattr(target, field) == expected
    assert any(field in w and "缺失" in w for w in _fixed(warns)), warns


def test_numeric_n_is_coerced_not_discarded(fig1):
    """`n: 3` 是类型错，但信息是真的，不该被降级成 not_reported。"""
    fig1["panels"][2]["groups"][0]["n"] = 3
    rec, warns = validate_record(fig1)
    assert rec is not None
    assert rec.panels[2].groups[0].n == "3"
    assert any("groups[0].n" in w for w in _fixed(warns))


def test_scalar_where_list_expected_is_wrapped(fig1):
    fig1["panels"][0]["design"]["levels"] = "0-4.5 μM 梯度"
    rec, warns = validate_record(fig1)
    assert rec is not None
    assert rec.panels[0].design.levels == ["0-4.5 μM 梯度"]
    assert _fixed(warns)


def test_illegal_role_falls_back_to_reference(fig1):
    fig1["panels"][0]["groups"][0]["role"] = "sham"
    rec, warns = validate_record(fig1)
    assert rec is not None
    assert rec.panels[0].groups[0].role == "reference", "非法角色应退到最中性的 reference"
    assert any("role" in w and "sham" in w for w in _fixed(warns))


def test_illegal_confidence_falls_back_to_low(fig1):
    fig1["confidence"] = "很高"
    rec, warns = validate_record(fig1)
    assert rec is not None
    assert rec.confidence.value == "low", "校验都没过的记录不配拿 medium"
    assert _fixed(warns)


def test_illegal_figure_kind_falls_back(fig1):
    fig1["figure_kind"] = "flowchart"
    rec, warns = validate_record(fig1)
    assert rec is not None
    assert rec.figure_kind == "experiment"
    assert _fixed(warns)


def test_broken_list_element_is_dropped(fig1):
    fig1["panels"][0]["groups"].insert(1, "这不是一个对象")
    n_before = len(fig1["panels"][0]["groups"])
    rec, warns = validate_record(fig1)
    assert rec is not None
    assert len(rec.panels[0].groups) == n_before - 1
    assert any("丢弃" in w for w in _fixed(warns))


def test_garbage_input_does_not_raise():
    """任何输入都不许抛异常——上游是模型输出，什么都可能来。"""
    for junk in ([], "figure", 42, None, {"figure_id": "Figure 1", "panels": "nope"}):
        rec, warns = validate_record(junk)  # type: ignore[arg-type]
        assert isinstance(warns, list) and warns
        assert rec is None or rec.figure_id


def test_deeply_wrong_types_converge(fig1):
    fig1["pages"] = "第四页"
    fig1["panels"][0]["evidence"]["inferred"] = {"a": 1}
    fig1["panels"][1]["design"] = 12345
    rec, warns = validate_record(fig1)
    assert rec is not None, "多处类型错也必须收敛，不能耗尽修复轮数"
    assert rec.pages == [] and rec.panels[0].evidence.inferred == []
    assert rec.panels[1].design.independent_var == NOT_REPORTED
    assert not _fatal(warns)


# ── validate_record：溯源审计（铁律的机器可检查部分） ──────────────────────


def test_unsourced_n_triggers_review(fig1):
    """填了 n 却没在 evidence 里溯源 —— 最典型的脑补现场。"""
    panel = fig1["panels"][0]
    panel["groups"][0]["n"] = "3"
    panel["evidence"]["inferred"] = []
    rec, warns = validate_record(fig1)
    assert rec is not None
    assert any("n 是最高危字段" in w for w in _review(warns))


def test_sourced_n_passes_audit(fig1):
    """正确做法（值自带依据 + inferred 列出 groups[].n）不该被误报。"""
    panel = fig1["panels"][2]  # 黄金样本里唯一填了 n 的 panel
    assert panel["groups"][0]["n"] == "3（图中散点为 3 个）"
    _, warns = validate_record(fig1)
    assert not [w for w in _review(warns) if "最高危字段" in w]


def test_prompt_counter_example_is_caught_by_the_validator(fig1):
    """提示词里的 ❌ 反例，校验器必须真的抓得到——否则那段反例只是装饰。

    反例的四处错误：n 无依据、stats.test 脑补、error_bar 编 SD、inferred 谎报为空。
    """
    panel = fig1["panels"][2]  # 黄金样本里做对了的那个 panel
    panel["groups"][0]["n"] = "3"  # 去掉「（图中散点为 3 个）」
    panel["stats"]["test"] = "t-test"
    panel["stats"]["error_bar"] = "SD"
    panel["evidence"]["inferred"] = []

    rec, warns = validate_record(fig1)
    assert rec is not None, "反例结构上合法，不该被当成坏数据丢掉"
    review = _review(warns)
    assert any("最高危字段" in w for w in review), "没抓到无溯源的 n"
    assert any("stats.test" in w for w in review), "没抓到脑补的统计检验"


def test_unsourced_stats_test_triggers_review(fig1):
    fig1["panels"][0]["stats"]["test"] = "t-test"
    rec, warns = validate_record(fig1)
    assert rec is not None
    assert any("stats.test" in w for w in _review(warns))


@pytest.mark.parametrize(
    "entry,should_pass",
    [
        ("groups[].n", True),
        ("stats.replicates", True),
        ("n", True),
        ("每组样本量", True),
        ("groups[].name", False),  # 曾经的 bug：子串 `.n` 让组名糊弄过 n 的溯源检查
        ("design.independent_var", False),
        ("groups[].result", False),
    ],
)
def test_n_evidence_matching_is_not_fooled_by_neighbouring_fields(fig1, entry, should_pass):
    panel = fig1["panels"][0]
    panel["groups"][0]["n"] = "6"
    panel["evidence"] = {"from_image_only": [entry], "inferred": []}
    _, warns = validate_record(fig1)
    flagged = any("最高危字段" in w for w in _review(warns))
    assert flagged != should_pass, f"{entry!r} 的溯源判定错了"


@pytest.mark.parametrize(
    "entry,should_pass",
    [
        ("stats.test", True),
        ("统计检验方法", True),
        ("ANOVA + post hoc", True),
        ("stats.significance", False),  # 曾经的 bug：裸 `stats` 让显著性标记冒充检验方法
        ("test set 大小", False),  # mlcs 论文里 test 满地都是
        ("groups[].result", False),
    ],
)
def test_stats_test_evidence_matching_is_precise(fig1, entry, should_pass):
    panel = fig1["panels"][0]
    panel["stats"]["test"] = "two-way ANOVA"
    panel["evidence"] = {"from_methods": [entry], "inferred": []}
    _, warns = validate_record(fig1)
    flagged = any("stats.test" in w for w in _review(warns))
    assert flagged != should_pass, f"{entry!r} 的溯源判定错了"


def test_omitted_domain_is_flagged(fig1):
    """契约里 domain 默认 mlcs，漏填时 pydantic 静默通过——生物论文会整体填错 profile。"""
    del fig1["domain"]
    rec, warns = validate_record(fig1)
    assert rec is not None and rec.domain == "mlcs"
    assert any("domain 未填" in w for w in _review(warns))


def test_caption_reported_n_but_panel_says_not_reported(fig1):
    """漏填哨兵：图题收尾写了 N = 3，panel 却全填 not_reported。

    这是 Figure 4 真实踩到的坑——收尾段被吞进末尾 panel，A~C 白白漏掉图题已报告的 n。
    """
    fig1["caption"] += " " + FIG4_TRAILER
    for p in fig1["panels"]:
        for g in p["groups"]:
            g["n"] = NOT_REPORTED
    _, warns = validate_record(fig1)
    missed = [w for w in _review(warns) if "图题已报告却填成 not_reported" in w]
    assert len(missed) == len(fig1["panels"]), "每个 panel 都该被点名，收尾段适用于全图"
    assert "N = 3" in missed[0], "要把图题里的原文片段带出来，复核才能一眼确认"


def test_caption_reported_errorbar_and_significance(fig1):
    fig1["caption"] += " " + FIG4_TRAILER
    fig1["panels"][1]["stats"] = {
        "test": NOT_REPORTED, "error_bar": NOT_REPORTED,
        "replicates": NOT_REPORTED, "significance": NOT_REPORTED,
    }
    _, warns = validate_record(fig1)
    hit = [w for w in _review(warns) if "panels[1]" in w and "图题已报告" in w]
    assert hit and "stats.error_bar" in hit[0] and "stats.significance" in hit[0]


def test_no_missed_warning_when_panel_filled_it_in(fig1):
    """正确回填之后，漏填哨兵必须闭嘴。"""
    fig1["caption"] += " " + FIG4_TRAILER
    for p in fig1["panels"]:
        p["stats"]["error_bar"] = "SD（图题：mean ± SD）"
        p["stats"]["significance"] = "* p<0.05, ** p<0.01, *** p<0.001（图题定义）"
        p["evidence"].setdefault("from_caption", []).append("stats")
        for g in p["groups"]:
            g["n"] = "3（图题：N = 3）"
    _, warns = validate_record(fig1)
    assert not [w for w in _review(warns) if "图题已报告" in w]


def test_missed_warning_silent_on_captions_without_such_info(bundle):
    """黄金样本的图题里本来就没有 N / mean ± SD，不能凭空报警。"""
    for raw in bundle["figures"]:
        _, warns = validate_record(raw)
        assert not [w for w in _review(warns) if "图题已报告" in w]


def test_empty_evidence_triggers_review(fig1):
    fig1["panels"][1]["evidence"] = {}
    _, warns = validate_record(fig1)
    assert any("五个桶全为空" in w for w in _review(warns))


def test_schematic_with_panels_triggers_review(bundle):
    rec_raw = copy.deepcopy(bundle["figures"][0])
    rec_raw["figure_kind"] = "schematic"
    _, warns = validate_record(rec_raw)
    assert any("schematic" in w and "panels 非空" in w for w in _review(warns))


def test_experiment_without_panels_triggers_review(fig1):
    fig1["panels"] = []
    _, warns = validate_record(fig1)
    assert any("没有任何 panel" in w for w in _review(warns))


def test_unreported_risk_without_open_questions_triggers_review(fig1):
    fig1["open_questions"] = []
    _, warns = validate_record(fig1)
    assert any("open_questions 为空" in w for w in _review(warns))


# ── build_extraction_tasks ─────────────────────────────────────────────────


def test_build_tasks_writes_one_file_per_figure(prepped: Path):
    tasks = build_extraction_tasks(prepped)
    assert [t.name for t in tasks] == ["figure_01.md", "table_03.md"]
    assert (prepped / "extracted").is_dir(), "输出目录应预先建好，省得 agent 自己 mkdir"


def test_build_tasks_requires_context_json(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="prep"):
        build_extraction_tasks(tmp_path)


def test_task_file_is_self_contained(prepped: Path):
    """任务文件要能单独丢给一个没有别的上下文的 subagent。"""
    text = build_extraction_tasks(prepped)[0].read_text(encoding="utf-8")
    # 切图用绝对路径（agent 工具只认绝对路径），记录里的 images 仍是相对路径
    assert (prepped / "figures" / "fig_01.png").as_posix() in text
    assert '"images": [\n    "figures/fig_01.png"\n  ]' in text
    # 上下文原文、段号、Methods 都在
    assert "CCK-8 assay of cell viability" in text
    assert "§2.3¶1" in text and "§4.2" in text
    # 规则、正反例、自检清单、骨架全都内嵌
    for needle in ("铁律 1", "铁律 2", "❌ 反例", "提交前自检", "输出骨架"):
        assert needle in text, f"任务文件缺少「{needle}」"
    # 输出路径写清楚了
    assert "extracted/figure_01.json" in text


FIG4_TRAILER = (
    "* indicates p < 0.05, ** indicates p < 0.01, *** indicates p < 0.001. N = 3. "
    "The control group was used for comparison. Data are expressed as mean ± SD."
)


def _with_caption_parts(out: Path, preamble: str = "", trailer: str = "") -> None:
    """给合成 context 的第一张图补上图题总述 / 收尾。"""
    ctx = out / "context.json"
    packs = json.loads(ctx.read_text(encoding="utf-8"))
    packs[0]["caption_preamble"] = preamble
    packs[0]["caption_trailer"] = trailer
    ctx.write_text(json.dumps(packs, ensure_ascii=False, indent=2), encoding="utf-8")


def test_caption_trailer_gets_its_own_section(prepped: Path):
    """收尾段藏着 N / mean ± SD / 星号定义，必须单独成节且写明适用于全图。"""
    _with_caption_parts(prepped, trailer=FIG4_TRAILER)
    text = build_extraction_tasks(prepped)[0].read_text(encoding="utf-8")

    assert "图题收尾" in text and FIG4_TRAILER in text
    trailer_sec = text.split("图题收尾", 1)[1].split("\n## ", 1)[0]
    assert "适用于全图每一个 panel" in text.split("图题收尾", 1)[1][:200]
    assert "不属于最后那个子图" in trailer_sec
    assert "not_reported" in trailer_sec
    # 收尾必须排在子图描述之后，否则会被当成最后一个 panel 的描述
    assert text.index("子图描述") < text.index("图题收尾")


def test_caption_preamble_gets_its_own_section(prepped: Path):
    _with_caption_parts(prepped, preamble="All experiments were performed in U251 cells.")
    text = build_extraction_tasks(prepped)[0].read_text(encoding="utf-8")
    assert "图题总述" in text
    assert "All experiments were performed in U251 cells." in text
    # 总述在子图描述之前
    assert text.index("图题总述") < text.index("子图描述")


def test_absent_caption_parts_leave_no_empty_sections(prepped: Path):
    text = build_extraction_tasks(prepped)[0].read_text(encoding="utf-8")
    assert "图题总述" not in text and "图题收尾" not in text


@pytest.mark.parametrize("preamble,trailer", [("", ""), ("前言", ""), ("", "收尾"), ("前", "后")])
def test_section_numbers_are_always_contiguous(prepped: Path, preamble, trailer):
    """节号断档会让 SKILL.md / 规则里的「第 N 节」指错地方。"""
    _with_caption_parts(prepped, preamble, trailer)
    for task in build_extraction_tasks(prepped):
        nums = [
            int(m) for m in re.findall(r"^## (\d+)\. ", task.read_text(encoding="utf-8"), re.M)
        ]
        # 0 是「先做这两件事」的引导节，正文从 1 开始；只要求全程无断档
        assert nums == list(range(nums[0], nums[0] + len(nums))), f"{task.name} 节号断档：{nums}"
        assert len(nums) >= 9, f"{task.name} 节数异常：{nums}"


def test_rules_warn_about_caption_reported_fields(prepped: Path):
    text = build_extraction_tasks(prepped)[0].read_text(encoding="utf-8")
    assert "反过来的错误" in text
    assert "N = ..." in text or "N = 3" in text
    assert "漏填和编造一样是错的" in text or "和编造一样是错的" in text


def test_task_file_flags_low_confidence_and_table(prepped: Path):
    text = build_extraction_tasks(prepped)[1].read_text(encoding="utf-8")
    assert "切图置信度 `low`" in text
    assert "这是**表格**" in text
    assert "w/o retrieval | 54.8" in text, "表格文本网格要给模型做数字交叉校验"


def test_forced_profile_short_circuits_domain_detection(prepped: Path):
    text = build_extraction_tasks(prepped, profile="bio")[0].read_text(encoding="utf-8")
    assert "强制 profile：`bio`" in text
    assert '"domain": "bio"' in text


def test_task_prefills_paper_title_from_parsed(prepped: Path):
    text = build_extraction_tasks(prepped)[0].read_text(encoding="utf-8")
    assert '"title": "A Demo Paper"' in text


@pytest.mark.parametrize(
    "figure_id,expected",
    [
        ("Figure 1", "figure_01"),
        ("Figure 10", "figure_10"),
        ("Fig. 2", "fig_02"),
        ("Table 3", "table_03"),
        ("Figure S1", "figure_s1"),
        ("图 4", "图_04"),
        ("", "figure_xx"),
    ],
)
def test_record_slug(figure_id: str, expected: str):
    assert record_slug(figure_id) == expected


# ── merge_records ──────────────────────────────────────────────────────────


def _write_extracted(out: Path, slug: str, record: dict) -> None:
    (out / "extracted").mkdir(parents=True, exist_ok=True)
    (out / "extracted" / f"{slug}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_merge_follows_context_order(prepped: Path, bundle):
    fig, tab = copy.deepcopy(bundle["figures"][0]), copy.deepcopy(bundle["figures"][0])
    tab["figure_id"], tab["kind"] = "Table 3", "table"
    # 故意先写 Table，靠 context.json 的顺序纠正
    _write_extracted(prepped, "table_03", tab)
    _write_extracted(prepped, "figure_01", fig)

    merged = merge_records(prepped)
    assert [f.figure_id for f in merged.figures] == ["Figure 1", "Table 3"]
    assert merged.source_pdf == "D:/papers/demo.pdf"
    assert merged.output_dir == str(prepped.resolve())
    assert merged.domain == "bio"
    assert (prepped / "figures.json").is_file()
    assert not _fatal(merged.warnings)


def test_merge_reports_missing_figures(prepped: Path, fig1):
    _write_extracted(prepped, "figure_01", fig1)
    merged = merge_records(prepped)
    assert len(merged.figures) == 1
    assert any("还没抽" in w and "Table 3" in w for w in _fatal(merged.warnings))


def test_merge_backfills_figure_id_from_filename(prepped: Path, fig1):
    fig1.pop("figure_id")
    _write_extracted(prepped, "figure_01", fig1)
    merged = merge_records(prepped)
    assert [f.figure_id for f in merged.figures] == ["Figure 1"]
    assert any("按文件名补为" in w for w in _fixed(merged.warnings))


def test_merge_unwraps_accidental_bundle(prepped: Path, bundle):
    _write_extracted(prepped, "figure_01", copy.deepcopy(bundle))
    merged = merge_records(prepped)
    assert len(merged.figures) == 3
    assert any("写成了 bundle" in w for w in _fixed(merged.warnings))


def test_merge_skips_unreadable_file_without_dying(prepped: Path, fig1):
    _write_extracted(prepped, "figure_01", fig1)
    (prepped / "extracted" / "table_03.json").write_text("{ 这不是 JSON", encoding="utf-8")
    merged = merge_records(prepped)
    assert len(merged.figures) == 1
    assert any("读取失败" in w for w in _fatal(merged.warnings))


def test_merge_empty_dir_is_reported_not_crashed(prepped: Path):
    merged = merge_records(prepped)
    assert merged.figures == []
    assert any("没有任何抽取结果" in w for w in _fatal(merged.warnings))


def test_merge_flags_domain_disagreement(prepped: Path, bundle):
    fig, tab = copy.deepcopy(bundle["figures"][0]), copy.deepcopy(bundle["figures"][0])
    tab["figure_id"], tab["domain"] = "Table 3", "mlcs"
    _write_extracted(prepped, "figure_01", fig)
    _write_extracted(prepped, "table_03", tab)
    merged = merge_records(prepped)
    assert any("domain 判定不一致" in w for w in _review(merged.warnings))


def test_merge_output_is_renderable_by_cli(prepped: Path, fig1):
    """figures.json 必须能被主 CLI 的 PaperBundle.model_validate_json 直接吃下。"""
    from pfn.models import PaperBundle

    _write_extracted(prepped, "figure_01", fig1)
    merge_records(prepped)
    reloaded = PaperBundle.model_validate_json(
        (prepped / "figures.json").read_text(encoding="utf-8")
    )
    assert reloaded.figures[0].figure_id == "Figure 1"


# ── 模式 B 接口 ────────────────────────────────────────────────────────────


def test_api_mode_fails_loudly_with_instructions():
    pack = ContextPack(figure_id="Figure 1", kind="figure", caption="c", images=[], pages=[1])
    with pytest.raises(NotImplementedError, match="模式 A"):
        extract_via_api(pack, [])


# ── 提示词漂移检测 ─────────────────────────────────────────────────────────

#: 这些措辞是防幻觉的承重墙，extract.py 与 SKILL.md 都必须保留。
LOAD_BEARING = [
    "not_reported",
    "严禁猜测",
    "groups[].n",
    "stats.test",
    "领域常识",
    "3（图中散点为 3 个）",
    "inferred",
]


def test_rules_keep_anti_hallucination_wording():
    blob = EXTRACTION_RULES + FEW_SHOT + SELF_CHECK
    for phrase in LOAD_BEARING:
        assert phrase in blob, f"提示词丢了承重措辞：{phrase}"


def test_skill_md_mirrors_the_rules():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    for phrase in LOAD_BEARING:
        assert phrase in skill, f"SKILL.md 与 extract.py 的规则漂移了，缺：{phrase}"


def test_skill_md_has_frontmatter_and_triggers():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\n")
    head = skill.split("---", 2)[1]
    assert "name: paper-figure-notes" in head
    assert "short-description" in head
    for trigger in ("文献整理", "论文精读", "figure", "实验设计"):
        assert trigger in head, f"description 少了触发词：{trigger}"
