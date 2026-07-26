"""L4 渲染层 —— 精读笔记 `figure-notes.md`。

纯函数：输入只有 PaperBundle，不读 PDF、不调模型，改排版可单独重跑。

排版目标是「读完这份笔记就不用回去翻原文」，所以每个 panel 固定给出五块：
实验设计 → 分组与结果 → 统计 → 结论 → 溯源。溯源里的 ⚠ 推断字段是人工
复核的入口，会再汇总到文末的复核清单。

两条硬约束（见 PLAN.md「两条铁律」）：
    1. `not_reported` 永远不上纸面，一律显示为「未说明」——区分「原文没写」
       与「工具没抽到」，前者是论文本身的缺陷，值得被看见。
    2. schematic（机制示意图）没有 panels，单独排版，绝不出现空表格。
"""

from __future__ import annotations

import re
from pathlib import Path

from pfn.models import (
    Evidence,
    FigureRecord,
    Group,
    Panel,
    PaperBundle,
    ReviewReason,
    is_reported,
    review_reasons,
)

NOTES_FILENAME = "figure-notes.md"

#: 原文未交代时的统一显示文案
MISSING = "未说明"

DOMAIN_LABEL = {"bio": "生物医学", "mlcs": "机器学习 / 计算机"}

FIGURE_KIND_LABEL = {
    "experiment": "实验数据",
    "schematic": "机制示意图",
    "qualitative_example": "定性示例",
    "analysis": "分析 / 组学",
}

CONFIDENCE_LABEL = {"high": "高", "medium": "中", "low": "低"}

ROLE_LABEL = {
    "treatment": "处理组",
    "control": "对照组",
    "baseline": "基线",
    "ablation": "消融",
    "reference": "参照",
}

#: 角色字形，与思维导图统一，一眼区分处理组与对照组
ROLE_GLYPH = {
    "treatment": "●",
    "control": "○",
    "baseline": "◎",
    "ablation": "◆",
    "reference": "▷",
}

#: 分组表的表头随领域切换填法（bio 看药物剂量，mlcs 看模型配置）
GROUP_HEADERS = {
    "bio": ["组名", "角色", "处理条件", "n", "测量指标", "结果", "vs 对照"],
    "mlcs": ["组名", "角色", "配置（模型/数据/超参）", "重复数", "评测指标", "结果", "vs 基线"],
}

#: 四列速览表的表头。一张 Figure 一张，用于一屏看完这张图做了什么。
SUMMARY_HEADERS = ["小图", "实验方法", "分组", "结果"]

TABLES_FILENAME = "figure-tables.md"

#: 只有带硬统计标记的 `vs_control` 才并进结果列——「相对对照下降 45%」这类
#: 描述性对比留在详细表里，并进来会把速览列撑爆。
_SIGNIF_RE = re.compile(r"\*|[pP]\s*[<>=≤≥]")

#: 复核理由三路的展示名。`kind` 是枚举，改这里的文案不影响判定逻辑。
REASON_KIND_LABEL = {
    "inferred": "模型自报推断",
    "low_confidence": "自评低置信",
    "sentinel": "校验哨兵",
}

#: 溯源行的展示顺序；inferred 单独处理，要显眼
EVIDENCE_SOURCES = (
    ("图题", "from_caption"),
    ("正文", "from_body"),
    ("Methods", "from_methods"),
    ("读图", "from_image_only"),
)

_LABEL_PREFIX = re.compile(
    r"^\s*(?:fig(?:ure)?|table|图|表)\s*\.?\s*[0-9IVXivx]+\s*[.:：、]?\s*",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.。;；!?])\s+")


# ── 通用小工具 ──────────────────────────────────────────────────────────────


def _dw(text: str) -> int:
    """显示宽度：CJK 与全角标点按 2 个字符宽算，用于截断。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _truncate(text: str, max_width: int) -> str:
    if _dw(text) <= max_width:
        return text
    kept, width = [], 0
    for ch in text:
        cw = 2 if ord(ch) > 0x2E7F else 1
        if width + cw > max_width - 1:
            break
        kept.append(ch)
        width += cw
    cut = "".join(kept)
    tail = text[len(cut) : len(cut) + 1]
    # 中文可随处断，英文不能把单词劈成两截（"experimenta…" 很难看）
    if cut and cut[-1].isascii() and cut[-1].isalnum() and tail.isalnum():
        cut = cut.rsplit(" ", 1)[0] or cut
    return _drop_dangling_bracket(cut).rstrip(" ，,、;；-") + "…"


#: 截断可能把左括号留在末尾（「柱状汇总（对应…」），回退到括号前更干净
_BRACKETS = (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"), ("《", "》"))


def _drop_dangling_bracket(text: str) -> str:
    for open_ch, close_ch in _BRACKETS:
        if text.count(open_ch) > text.count(close_ch):
            text = text[: text.rindex(open_ch)]
    return text


def _val(value: object) -> str:
    """Enum / str 都可能出现（pydantic 反序列化后是 Enum），统一取字面值。"""
    return str(getattr(value, "value", value) or "")


#: `<` 后面跟字母 / `/` / `!` / `?` 才是标签起始。生物医学图题里 `p<0.001`、
#: `log2FC < -1` 遍地都是，一律转义会把原文弄得没法读。
_MD_TAG_RE = re.compile(r"<(?=[A-Za-z/!?])")
_MD_ENTITY_RE = re.compile(r"&(?=[A-Za-z][A-Za-z0-9]*;|#\d+;|#x[0-9A-Fa-f]+;)")
#: 换行会捅穿标题、引用块和表格单元格——`_text()` 的下游全是单行上下文
_WS_RE = re.compile(r"\s+")


def _md_inline(text: str) -> str:
    """中和会破坏 .md 结构的字符。

    图题、组名、结果都是 PDF 或模型来的文本，直接写进 markdown 正文属于
    「不经过任何渲染器转义」的路径：一个 ``` 就能把围栏配对搞乱（笔记里
    内嵌了 mermaid 代码块，围栏错位会毁掉整个文件），`<script>` 则会被
    Obsidian 当裸 HTML 渲染。

    只动反引号、标签起始的 `<`、实体形式的 `&` 三类，其余原样保留——
    `()` `%` `*` 在正文里都是合法字符，套 mermaid 那张全角替换表会毁掉原文。
    **图题的主要用途是拿去原文 PDF 里 Ctrl+F 定位，改一个字符就搜不到了**，
    所以宁可放过也不多转。实测真实图题（含 `log2FC > 1`、`p<0.05`、`Nrf2 & HMOX1`）
    逐字节保持不变。

    `&` 那条是**保真而非安全**：markdown-it 与 python-markdown 实测都把
    `&lt;script&gt;` 原样输出为转义文本，不会解码成活标签；转它只是为了让
    原文里本就写着 `&lt;b&gt;` 的图题渲染出来仍是 `&lt;b&gt;` 而不是 `<b>`。
    """
    text = _WS_RE.sub(" ", text).strip()
    text = text.replace("`", "\\`")
    # 实体必须先处理：否则下一步产生的 `&lt;` 会被自己再转义成 `&amp;lt;`
    text = _MD_ENTITY_RE.sub("&amp;", text)
    return _MD_TAG_RE.sub("&lt;", text)


def _text(value: object, default: str = MISSING) -> str:
    """not_reported / 空 → 默认文案。绝不把 `not_reported` 打到纸面上。

    这里是模型/PDF 文本进入文档的主要入口，转义放在此处，后续拼接自己加的
    `<br>` 等标记不受影响。
    """
    if not is_reported(value):
        return default
    return _md_inline(str(value).strip())


def _join(items: object, sep: str = "；", default: str = MISSING) -> str:
    if not is_reported(items):
        return default
    parts = [_md_inline(str(i).strip()) for i in items if str(i).strip()]  # type: ignore[union-attr]
    return sep.join(parts) if parts else default


def _cell(text: str) -> str:
    """表格单元格转义：竖线会截断列，换行会破表。"""
    return text.replace("|", "\\|").replace("\n", "<br>").strip()


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out + [""]


def _code_list(items: list[str]) -> str:
    # 反引号包裹的内容里再出现反引号会把代码跨切断，直接去掉
    return ", ".join(f"`{str(i).replace('`', '')}`" for i in items if str(i).strip())


def _image_link(alt: str, path: str) -> str:
    """图片相对路径直出，Obsidian / Typora 可直接显示；带空格的路径要用尖括号包。"""
    rel = str(path).replace("\\", "/").strip()
    if any(ch in rel for ch in " ()"):
        rel = f"<{rel}>"
    return f"![{alt}]({rel})"


def _caption_title(caption: str, figure_id: str) -> str:
    """从图题里取一句话做标题：去掉 `Fig. 1.` 前缀，取首句，截断。

    首选 render_mindmap 的 `figure_title()`——它对缩写（`e.g.`）、属名
    （`A. baumannii`）、小数（`5.148`）、`FIGURE 1 |` 这些断句陷阱都有覆盖，
    本地实现曾在前两种上把标题截成「Effects on A」。导入不到时退回本地版。
    """
    caption = (caption or "").strip()
    if not caption:
        return ""
    try:
        from pfn.models import FigureRecord
        from pfn.render_mindmap import figure_title

        first = figure_title(FigureRecord(figure_id=figure_id, caption=caption))
    except Exception:  # noqa: BLE001 — 标题降级不该让整份笔记失败
        first = ""
    if not first:
        body = _LABEL_PREFIX.sub("", caption)
        if not body:
            return ""
        first = _SENTENCE_END.split(body, maxsplit=1)[0].strip().rstrip(".。")
    return _truncate(_md_inline(first), 78)


def _pages_label(pages: list[int]) -> str:
    if not pages:
        return MISSING
    return "p" + ", ".join(str(p) for p in pages)


# ── 文档头 ──────────────────────────────────────────────────────────────────


def _doc_title(bundle: PaperBundle) -> str:
    if bundle.paper.title.strip():
        return _md_inline(bundle.paper.title.strip())
    for fig in bundle.figures:
        if fig.paper.title.strip():
            return _md_inline(fig.paper.title.strip())
    return "（未命名论文）"


def _header(bundle: PaperBundle) -> list[str]:
    meta = bundle.paper
    domain = _val(bundle.domain)
    rows = [
        ["期刊 / 会议", _text(meta.venue)],
        ["年份", _text(meta.year)],
        ["DOI", f"[{meta.doi}](https://doi.org/{meta.doi})" if meta.doi.strip() else MISSING],
        ["领域 profile", f"{DOMAIN_LABEL.get(domain, domain)}（`{domain}`）"],
        ["源文件", f"`{bundle.source_pdf}`" if bundle.source_pdf else MISSING],
    ]

    n_fig = len(bundle.figures)
    n_panel = sum(len(f.panels) for f in bundle.figures)
    n_group = sum(len(p.groups) for f in bundle.figures for p in f.panels)
    rows.append(["规模", f"{n_fig} 图 · {n_panel} 个 panel · {n_group} 个实验组"])

    lines = [f"# {_doc_title(bundle)} — figure 精读笔记", ""]
    lines += _table(["项", "内容"], rows)
    lines += [
        "> **怎么读这份笔记**",
        "> · ⚠ = 需人工复核。三路独立判定：模型自报的推断字段、自报的低置信度、"
        "以及校验哨兵的独立复核——**自报「无推断」不等于可信**。全部条目汇总在"
        "文末[复核清单](#复核清单)。  ",
        f"> · 「{MISSING}」= **原文未交代**，不是提取遗漏；n / 剂量 / 统计检验三项尤其常见，"
        "本工具不做任何脑补。  ",
        "> · 图片为相对路径，笔记与 `figures/` 目录需保持同级。",
        "",
        "---",
        "",
    ]
    return lines


def _has_sentinel(reasons: list[ReviewReason]) -> bool:
    return any(r.kind == "sentinel" for r in reasons)


def _panel_reasons(reasons: list[ReviewReason], label: str) -> list[ReviewReason]:
    """归属到某个 panel 的理由。`panel is None` 的是图级理由，不算在内。"""
    return [r for r in reasons if r.panel is not None and r.panel == label]


def _reason_line(reason: ReviewReason) -> str:
    """图级展示用：标出归属，读者一眼知道该核哪个 panel。

    只有 sentinel 需要额外冠上来源标签——它的 text 讲的是「填了什么却没溯源」，
    不会自报是哪一路查出来的，而「独立于模型自报」正是它值得优先看的原因。
    另外两路的 text 本身就自述了来源（「含模型推断字段…」「整图置信度自评为低」），
    再加标签就成了同一句话说两遍。
    """
    scope = f"Panel {reason.panel}" if reason.panel else "整图"
    if reason.kind == "sentinel":
        return f"{scope} · **{REASON_KIND_LABEL['sentinel']}**：{reason.text}"
    return f"{scope} · {reason.text}"


def _confidence_cell(fig: FigureRecord, reasons: list[ReviewReason]) -> str:
    """自报 high 却被哨兵抓住 → `高 ⚠`。这种自相矛盾本身就是危险信号。"""
    label = CONFIDENCE_LABEL.get(_val(fig.confidence), _val(fig.confidence))
    if _val(fig.confidence) == "high" and _has_sentinel(reasons):
        return f"{label} ⚠"
    return label


def _overview(bundle: PaperBundle, all_reasons: dict[str, list[ReviewReason]]) -> list[str]:
    if not bundle.figures:
        return ["> 本篇未提取到任何图。", "", "---", ""]

    rows = []
    for fig in bundle.figures:
        n_group = sum(len(p.groups) for p in fig.panels)
        reasons = all_reasons.get(fig.figure_id, [])
        # 只看 panel 自报的 inferred 会漏掉「谎称零推断」的记录，必须走 review_reasons
        review = ("⚠ 哨兵" if _has_sentinel(reasons) else "⚠") if reasons else ""
        rows.append(
            [
                f"**{fig.figure_id}**",
                FIGURE_KIND_LABEL.get(_val(fig.figure_kind), _val(fig.figure_kind)),
                _truncate(_text(fig.question), 60),
                str(len(fig.panels)) if fig.panels else "—",
                str(n_group) if n_group else "—",
                _confidence_cell(fig, reasons),
                review,
            ]
        )

    lines = ["## 全图速览", ""]
    lines += _table(["图", "类型", "这张图要回答的问题", "panel", "实验组", "置信度", "复核"], rows)
    lines += ["---", ""]
    return lines


# ── panel ───────────────────────────────────────────────────────────────────


def _design_block(panel: Panel) -> list[str]:
    d = panel.design
    items = [
        ("自变量", _text(d.independent_var, "")),
        ("水平（= 分组依据）", _join(d.levels, " / ", "")),
        ("受控条件", _join(d.controlled, "；", "")),
        ("实验单位", _text(d.unit, "")),
    ]
    shown = [(k, v) for k, v in items if v]
    if not shown:
        return ["**实验设计** 原文未给出可提取的设计信息。", ""]
    return ["**实验设计**", ""] + [f"- **{k}**：{v}" for k, v in shown] + [""]


def _groups_block(panel: Panel, domain: str) -> list[str]:
    if not panel.groups:
        return [
            "**分组与结果** 该 panel 未抽取到分组"
            "（可能为定性展示，或原文未给出可分组的处理条件）。",
            "",
        ]
    headers = GROUP_HEADERS.get(domain, GROUP_HEADERS["bio"])
    rows = []
    for g in panel.groups:
        role = _val(g.role)
        rows.append(
            [
                f"**{_text(g.name, '—')}**",
                ROLE_LABEL.get(role, role),
                _text(g.condition),
                _text(g.n),
                _text(g.readout),
                _text(g.result),
                _text(g.vs_control),
            ]
        )
    return ["**分组与结果**", ""] + _table(headers, rows)


def _stats_block(panel: Panel) -> list[str]:
    s = panel.stats
    items = [
        ("检验", _text(s.test, "")),
        ("误差棒", _text(s.error_bar, "")),
        ("重复", _text(s.replicates, "")),
        ("显著性标注", _text(s.significance, "")),
    ]
    shown = [f"{k}：{v}" for k, v in items if v]
    if not shown:
        return ["**统计** 原文未说明检验方法、误差棒类型与重复数。", ""]
    missing = [k for k, v in items if not v]
    line = "**统计** " + " · ".join(shown)
    if missing:
        line += f" · （{'、'.join(missing)}{MISSING}）"
    return [line, ""]


def _evidence_block(ev: Evidence, reasons: list[ReviewReason]) -> list[str]:
    parts = []
    for label, attr in EVIDENCE_SOURCES:
        values = getattr(ev, attr)
        if values:
            parts.append(f"{label} → {_code_list(values)}")
    line = "｜".join(parts) if parts else "未记录来源"
    if ev.inferred:
        line += f"｜⚠ **模型推断 → {_code_list(ev.inferred)}**"
    lines = [f"> **溯源** {line}"]

    # 哨兵理由和溯源是同一件事的两面（「填了值却没记来源」），并进同一个引用块。
    # inferred 那路不再重复展示——上面的「模型推断 →」已经把字段路径列全了。
    for reason in reasons:
        if reason.kind != "inferred":
            lines += [">", f"> ⚠ **{REASON_KIND_LABEL.get(reason.kind, reason.kind)}** {reason.text}"]
    return lines + [""]


def _panel_name(panel: Panel) -> str:
    label = panel.label.strip()
    return f"Panel {label}" if label and label not in {"-", "—", "–"} else "整图（未分子图）"


# ── 四列速览表 ──────────────────────────────────────────────────────────────


def _common_readout(panel: Panel) -> str:
    """各组测量指标一致时返回它，否则空——不一致时并进「实验方法」会误导。"""
    readouts = {_text(g.readout, "") for g in panel.groups}
    readouts.discard("")
    return readouts.pop() if len(readouts) == 1 else ""


def _method_cell(panel: Panel) -> str:
    experiment = _text(panel.experiment, "")
    cell = _truncate(experiment, 34) if experiment else MISSING
    readout = _common_readout(panel)
    if readout and readout not in experiment:
        cell += f"<br>指标：{_truncate(readout, 30)}"
    return cell


_TOKEN_RE = re.compile(r"[0-9A-Za-z_.+-]+|[一-鿿]+")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _condition_adds_info(name: str, condition: str) -> bool:
    """组名已经把处理条件说清楚时就别再重复一遍。

    速览表最怕的是同一句话在四行里各写一次（`U251 · 12 h` 配
    `DMC-GF 0-4.5 μM 梯度，处理 12 h`），列被撑宽反而看不动。
    组名的词有六成以上出现在条件里，就认为条件没有新信息。
    """
    if not condition or name in condition or condition in name:
        return False
    name_tokens = _tokens(name)
    if not name_tokens:
        return True
    return len(name_tokens & _tokens(condition)) / len(name_tokens) < 0.6


def _group_cell(group: Group) -> str:
    """角色字形 + 组名 + 关键处理条件，压成一行。"""
    role = _val(group.role)
    name = _text(group.name, "—")
    cell = f"{ROLE_GLYPH.get(role, '')} {name}".strip()
    condition = _text(group.condition, "")
    if _condition_adds_info(name, condition):
        detail = _truncate(condition, 32)
        # 组名自带括号时（`上调基因（up）`）再套一层会连着两组括号，改用间隔号
        cell += f" · {detail}" if name.rstrip().endswith(("）", ")")) else f"（{detail}）"
    return cell


def _result_cell(group: Group) -> str:
    result = _text(group.result)
    vs = _text(group.vs_control, "")
    if vs and _SIGNIF_RE.search(vs):
        result += f"（{vs}）" if result != MISSING else f"{vs}"
    return result


def figure_summary_rows(
    fig: FigureRecord, reasons: list[ReviewReason], blank_repeats: bool = True
) -> list[list[str]]:
    """四列速览表的行：小图 / 实验方法 / 分组 / 结果，**一行一个 group**。

    `blank_repeats=True`（markdown 用）时同一 panel 的后续行留空前两列——
    markdown 没有 rowspan，留空是最干净的读法。写 Excel 时传 False 填满，
    否则空单元格会让排序和筛选错位。
    """
    rows: list[list[str]] = []
    for panel in fig.panels:
        mark = "⚠ " if _panel_reasons(reasons, panel.label) else ""
        label = panel.label.strip()
        head = f"{mark}{label}" if label and label not in {"-", "—", "–"} else f"{mark}整图"
        method = _method_cell(panel)

        if not panel.groups:
            rows.append([head, method, MISSING, MISSING])
            continue
        for i, group in enumerate(panel.groups):
            first = i == 0 or not blank_repeats
            rows.append(
                [head if first else "", method if first else "", _group_cell(group), _result_cell(group)]
            )
    return rows


def _summary_table(fig: FigureRecord, reasons: list[ReviewReason]) -> list[str]:
    rows = figure_summary_rows(fig, reasons)
    if not rows:
        return []
    return ["**速览**（详细分组见下方各 panel）", ""] + _table(SUMMARY_HEADERS, rows)


def _panel_section(panel: Panel, domain: str, reasons: list[ReviewReason]) -> list[str]:
    # panel 级 ⚠ 覆盖三路：自报推断、哨兵命中本 panel 等。只看 evidence.inferred
    # 会漏掉哨兵抓到的脑补记录——那正是最该标的一类。
    mark = "⚠ " if reasons else ""

    experiment = _text(panel.experiment, "")
    heading = f"### {mark}{_panel_name(panel)}"
    if experiment:
        heading += f" · {_truncate(experiment, 72)}"

    lines = [heading, ""]
    if experiment and _dw(experiment) > 72:
        lines += [f"**实验** {experiment}", ""]
    elif not experiment:
        lines += [f"**实验** {MISSING}", ""]

    lines += _design_block(panel)
    lines += _groups_block(panel, domain)
    lines += _stats_block(panel)
    lines += [f"**本 panel 结论** {_text(panel.finding)}", ""]
    lines += _evidence_block(panel.evidence, reasons)
    return lines


# ── figure ──────────────────────────────────────────────────────────────────


def _figure_images(fig: FigureRecord) -> list[str]:
    if not fig.images:
        return ["> （无切图，可能是纯文本表格或切图失败）", ""]
    if len(fig.images) == 1:
        return [_image_link(fig.figure_id, fig.images[0]), ""]

    # 跨页图：`Fig. N (continued)` 合并成一条记录，多张切图全部列出
    lines = [f"> 本图跨 {len(fig.pages) or len(fig.images)} 页，共 {len(fig.images)} 张切图。", ""]
    for i, img in enumerate(fig.images, 1):
        page = fig.pages[i - 1] if i - 1 < len(fig.pages) else None
        alt = f"{fig.figure_id} · 第 {i}/{len(fig.images)} 张"
        lines += [_image_link(alt + (f"（p{page}）" if page else ""), img), ""]
    return lines


def _figure_mindmap(fig: FigureRecord, reasons: list[str]) -> list[str]:
    """PLAN §5 ②：单图分组结构导图嵌在该图下方，用于快速回忆这张图的设计。

    走 render_mindmap 的免落盘入口。`reasons` 必须传：不传会退化成只看
    FigureRecord 自报的信息，谎称 `inferred: []` 的脑补记录就会在导图里
    漏标——那样嵌在本节里的导图会和上方的 ⚠ 自相矛盾。

    导图是锦上添花，渲染不出来也不该让整份笔记失败，因此对导入与渲染都做兜底。
    """
    if not fig.panels:
        return []
    try:
        from pfn.render_mindmap import render_figure_mindmap

        block = render_figure_mindmap(fig, depth="group", reasons=reasons)
    except Exception:  # noqa: BLE001 — 导图缺失不影响笔记主体
        return []
    if not block.strip():
        return []
    return ["**分组结构导图**", "", block.rstrip(), ""]


def _review_callout(reasons: list[ReviewReason]) -> list[str]:
    """图级复核入口：列全部理由并标出归属，读者一眼知道要核哪个 panel。"""
    if not reasons:
        return []
    if len(reasons) == 1:
        return [f"> ⚠ **需人工复核** {_reason_line(reasons[0])}", ""]
    # 多条理由用引用块内的列表，免得渲染器把连续的 `>` 行并成一段
    return ["> ⚠ **需人工复核**", ">"] + [f"> - {_reason_line(r)}" for r in reasons] + [""]


def _figure_section(fig: FigureRecord, reasons: list[ReviewReason]) -> list[str]:
    kind = _val(fig.figure_kind)
    domain = _val(fig.domain)
    mark = "⚠ " if reasons else ""
    title = _caption_title(fig.caption, fig.figure_id)

    lines = [f"## {mark}{fig.figure_id}" + (f" · {title}" if title else ""), ""]

    meta = [
        FIGURE_KIND_LABEL.get(kind, kind),
        _pages_label(fig.pages),
        f"置信度 {_confidence_cell(fig, reasons)}",
    ]
    if fig.panels:
        n_group = sum(len(p.groups) for p in fig.panels)
        meta.append(f"{len(fig.panels)} 个 panel · {n_group} 个实验组")
    lines += [" · ".join(meta), ""]

    lines += _figure_images(fig)
    lines += ["**图题原文**", "", f"> {_text(fig.caption, '（原文图题缺失）')}", ""]
    lines += [f"**这张图要回答的问题** {_text(fig.question)}", ""]
    lines += _review_callout(reasons)

    if kind == "schematic":
        # 示意图无实验分组，走单独排版，不出空表
        lines += [
            "> **本图为示意图**，不含实验数据与分组，无 panel 级实验设计。",
            "",
            f"**图示内容** {_text(fig.figure_conclusion)}",
            "",
        ]
    elif not fig.panels:
        lines += [
            "> 本图未拆分出 panel（可能为整图单一实验，或抽取时未能识别子图）。",
            "",
            f"**整图结论** {_text(fig.figure_conclusion)}",
            "",
        ]
    else:
        # 先速览再下钻：四列表在前，各 panel 的详细分组在后
        lines += _summary_table(fig, reasons)
        for panel in fig.panels:
            lines += _panel_section(panel, domain, _panel_reasons(reasons, panel.label))
        lines += [f"**整图结论** {_text(fig.figure_conclusion)}", ""]

    if fig.open_questions:
        lines += ["**存疑点**（原文未交代、影响可重复性）", ""]
        lines += [f"{i}. {q}" for i, q in enumerate(fig.open_questions, 1)]
        lines += [""]

    # 导图是整节的收尾速览，放最后，免得和上面的正文逐条撞车
    lines += _figure_mindmap(fig, reasons)
    lines += ["---", ""]
    return lines


# ── 复核清单 ────────────────────────────────────────────────────────────────


def _completeness(bundle: PaperBundle) -> list[str]:
    """n / 统计检验 / 误差棒的报告率。这三项最容易被模型脑补，也最该被看见缺失。"""
    groups = [g for f in bundle.figures for p in f.panels for g in p.groups]
    panels = [p for f in bundle.figures for p in f.panels]
    if not groups and not panels:
        return []

    def ratio(reported: int, total: int) -> str:
        return f"{reported} / {total}" if total else "—"

    rows = [
        [
            "样本量 n",
            ratio(sum(1 for g in groups if is_reported(g.n)), len(groups)),
            "实验组",
        ],
        [
            "统计检验",
            ratio(sum(1 for p in panels if is_reported(p.stats.test)), len(panels)),
            "panel",
        ],
        [
            "误差棒类型",
            ratio(sum(1 for p in panels if is_reported(p.stats.error_bar)), len(panels)),
            "panel",
        ],
        [
            "重复数",
            ratio(sum(1 for p in panels if is_reported(p.stats.replicates)), len(panels)),
            "panel",
        ],
    ]
    lines = ["**关键字段报告率**（分母为总数；未报告 = 原文没写，不是提取失败）", ""]
    lines += _table(["字段", "已报告", "统计口径"], rows)
    return lines


def _review_checklist(
    bundle: PaperBundle, all_reasons: dict[str, list[ReviewReason]]
) -> list[str]:
    """全文 ⚠ 的落点。判定口径与速览表、各图小节完全一致，都走 `review_reasons()`。"""
    lines = ["## 复核清单", ""]

    rows = []
    for fig in bundle.figures:
        reasons = all_reasons.get(fig.figure_id, [])
        if not reasons:
            continue
        rows.append(
            [
                fig.figure_id,
                _confidence_cell(fig, reasons),
                "<br>".join(_reason_line(r) for r in reasons),
            ]
        )

    if rows:
        sentinel = sum(1 for f in bundle.figures if _has_sentinel(all_reasons.get(f.figure_id, [])))
        intro = (
            f"以下 {len(rows)} 张图需要回查原文。理由来自三路独立判定："
            "模型自报的推断字段、自报的低置信度、以及校验哨兵的独立复核。"
        )
        if sentinel:
            intro += (
                f"其中 **{sentinel} 张被哨兵单独抓出**——这类记录往往同时谎称"
                "`inferred: []` 并自报 high，**优先看它们**。"
            )
        lines += [intro, ""]
        lines += _table(["图", "置信度", "复核理由"], rows)
    else:
        lines += ["三路判定均未命中，本次提取无需人工复核。", ""]

    # medium 不触发 ⚠（否则满屏都是），但值得单独点名
    medium = [
        f.figure_id
        for f in bundle.figures
        if _val(f.confidence) == "medium" and f.figure_id not in all_reasons
    ]
    if medium:
        lines += [
            f"另有 {len(medium)} 张图置信度为「中」但未被上表命中，可择机抽查："
            f"{', '.join(medium)}。",
            "",
        ]

    lines += _completeness(bundle)
    return lines


def _warnings_section(bundle: PaperBundle) -> list[str]:
    if not bundle.warnings:
        return []
    lines = ["## 处理警告", ""]
    lines += [f"- {w}" for w in bundle.warnings]
    return lines + [""]


# ── 入口 ────────────────────────────────────────────────────────────────────


def render_figure_tables(bundle: PaperBundle, out_dir: Path) -> Path:
    """只含四列速览表的独立产物 `figure-tables.md`，一张图一节。

    与 `figure-notes.md` 里内嵌的同一张表同源（`figure_summary_rows`），
    这里是给「整篇扫读 / 贴进别处」用的精简版，不含实验设计与溯源。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    all_reasons = review_reasons(bundle)

    lines = [
        f"# {_doc_title(bundle)} — Figure 速览表",
        "",
        "> 每张图一张四列表：**小图 / 实验方法 / 分组 / 结果**。"
        f"⚠ = 需人工复核，「{MISSING}」= 原文未交代。",
        "> 完整的实验设计、统计与逐字段溯源见 [`figure-notes.md`](figure-notes.md)。",
        "",
    ]

    for fig in bundle.figures:
        reasons = all_reasons.get(fig.figure_id, [])
        title = _caption_title(fig.caption, fig.figure_id)
        lines += [
            f"## {'⚠ ' if reasons else ''}{fig.figure_id}" + (f" · {title}" if title else ""),
            "",
        ]
        rows = figure_summary_rows(fig, reasons)
        if rows:
            lines += _table(SUMMARY_HEADERS, rows)
        elif _val(fig.figure_kind) == "schematic":
            lines += ["本图为示意图，不含实验分组。", ""]
        else:
            lines += ["本图未抽取到分组数据。", ""]

    path = out_dir / TABLES_FILENAME
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")
    return path


def render_notes(bundle: PaperBundle, out_dir: Path) -> Path:
    """渲染精读笔记，返回 `figure-notes.md` 路径。

    同时写出 `figure-tables.md`（四列速览表的独立版本）。CLI 只收本函数的
    返回值，所以那个文件不会出现在产物清单里——如需列出，在 `cmd_render`
    里加一行 `written.append(render_figure_tables(bundle, out))`。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 全文 ⚠ 的唯一判定口径。三路理由（自报推断 / 自报低置信 / 哨兵）在这里
    # 一次算清，速览表、图小节、复核清单共用，避免各处标记打架。
    all_reasons = review_reasons(bundle)

    lines: list[str] = []
    lines += _header(bundle)
    lines += _overview(bundle, all_reasons)
    for fig in bundle.figures:
        lines += _figure_section(fig, all_reasons.get(fig.figure_id, []))
    lines += _review_checklist(bundle, all_reasons)
    lines += _warnings_section(bundle)

    path = out_dir / NOTES_FILENAME
    # Windows 默认 GBK + CRLF，两个都要显式关掉，与其余 L4 产物保持一致
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")

    render_figure_tables(bundle, out_dir)
    return path
