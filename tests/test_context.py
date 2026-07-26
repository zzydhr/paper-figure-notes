#!/usr/bin/env python
"""L2 装配层自测 —— 不依赖 L1，用合成的 ParsedPaper 打全部路径。

    python tests\\test_context.py            跑全部断言并打印一份 context md 供人工审阅
    python tests\\test_context.py --quiet     只跑断言

合成三篇论文，覆盖：
  A. 生物医学（期刊排版）：CCK-8 / 流式 / WB / ROS / 异种移植 / 统计，8 段 Methods
  B. ML/NLP（ACL 排版）：模型 / 数据集与指标 / 训练细节
  C. 中文论文：中文图表引用与中文图题子图切分

产物写到 tests/_out/，每次运行覆盖，可以直接打开读。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from pfn import context as ctx  # noqa: E402
from pfn.models import (  # noqa: E402
    Confidence,
    FigureAsset,
    LayoutRoute,
    Paragraph,
    ParsedPaper,
    Section,
)

OUT = ROOT / "tests" / "_out"

_failures: list[str] = []
_checks = 0


def check(cond: bool, msg: str) -> bool:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(msg)
        print(f"  FAIL  {msg}")
    return bool(cond)


def eq(actual, expected, msg: str) -> bool:
    return check(actual == expected, f"{msg}\n        期望 {expected!r}\n        实际 {actual!r}")


# ══════════════════════════════════════════════════════════════════════════
# 合成论文 A · 生物医学
# ══════════════════════════════════════════════════════════════════════════


def make_bio_paper() -> ParsedPaper:
    S = lambda i, t, lv, pg, m=False: Section(  # noqa: E731
        id=i, title=t, level=lv, page_start=pg, is_methods=m
    )
    P = lambda i, s, pg, t: Paragraph(id=i, section_id=s, page=pg, text=t)  # noqa: E731

    sections = [
        S("§1", "Introduction", 1, 1),
        S("§2", "Materials and Methods", 1, 2, True),
        S("§2.1", "Cell lines and reagents", 2, 2, True),
        S("§2.2", "CCK-8 cell viability assay", 2, 2, True),
        S("§2.3", "Flow cytometric analysis of apoptosis", 2, 2, True),
        S("§2.4", "Western blotting", 2, 3, True),
        S("§2.5", "Measurement of ROS, MDA and GSH", 2, 3, True),
        S("§2.6", "RNA sequencing", 2, 3, True),
        S("§2.7", "Subcutaneous xenograft model", 2, 3, True),
        S("§2.8", "Statistical analysis", 2, 3, True),
        S("§3", "Results", 1, 4),
        S("§3.1", "DMC-GF suppresses glioma cell proliferation", 2, 4),
        S("§3.2", "DMC-GF induces ferroptosis", 2, 5),
        S("§4", "Discussion", 1, 7),
    ]

    paragraphs = [
        P("§1¶1", "§1", 1,
          "Glioblastoma remains the most lethal primary brain tumour, and temozolomide "
          "resistance limits the benefit of standard therapy. Here we characterise the "
          "curcumin derivative DMC-GF; the proposed mechanism is summarised in Figure 3."),

        P("§2.1¶1", "§2.1", 2,
          "Human glioma cell lines U251 and U87 were obtained from the Chinese Academy of "
          "Sciences Cell Bank and maintained in DMEM supplemented with 10% FBS at 37 °C in "
          "5% CO2. DMC-GF was dissolved in DMSO to a 10 mM stock solution and diluted in "
          "medium immediately before use. Temozolomide (TMZ), erastin and ferrostatin-1 "
          "(Fer-1) were purchased from MedChemExpress. Anti-GPX4, anti-SLC7A11, anti-HMOX1 "
          "and anti-Nrf2 antibodies were from Abcam."),

        P("§2.2¶1", "§2.2", 2,
          "Cell viability was assessed with the CCK-8 assay. U251 and U87 cells were seeded "
          "into 96-well plates at 5 x 10^3 cells per well and treated with DMC-GF at 0, 0.5, "
          "1.5, 3.0 or 4.5 μM for 12, 24 or 48 h. Ten microlitres of CCK-8 reagent were added "
          "to each well and the absorbance at 450 nm was recorded after 2 h. IC50 values were "
          "fitted in GraphPad Prism 9. Three independent experiments were performed, each "
          "with six replicate wells."),

        P("§2.3¶1", "§2.3", 2,
          "Apoptosis was measured by flow cytometry with an Annexin V-FITC/propidium iodide "
          "kit. U251 cells were treated with 0, 1.5 or 3.0 μM DMC-GF for 24 h, harvested by "
          "trypsin digestion without EDTA, stained for 15 min in the dark and analysed on a "
          "BD FACSCalibur. Data are from three biological replicates."),

        P("§2.4¶1", "§2.4", 3,
          "For western blotting, cells were lysed in RIPA buffer containing protease "
          "inhibitors and protein concentration was determined with a BCA kit. Equal amounts "
          "of protein (30 μg) were separated by SDS-PAGE, transferred to PVDF membranes and "
          "probed overnight at 4 °C with anti-GPX4 (1:1000), anti-SLC7A11 (1:1000) and "
          "anti-HMOX1 (1:2000). Bands were visualised by ECL and quantified in ImageJ."),

        P("§2.5¶1", "§2.5", 3,
          "Intracellular ROS were measured with the DCFH-DA fluorescent probe after treatment "
          "with 3.0 μM DMC-GF for 24 h. MDA content and GSH levels were determined with "
          "commercial kits (Beyotime) according to the manufacturer's instructions and "
          "normalised to total protein. Each assay was repeated three times."),

        P("§2.6¶1", "§2.6", 3,
          "Total RNA was extracted with TRIzol and sequenced on an Illumina NovaSeq 6000. "
          "Differentially expressed genes were identified with DESeq2 and subjected to GO "
          "and KEGG enrichment analysis."),

        P("§2.7¶1", "§2.7", 3,
          "Four-week-old female BALB/c nude mice (n = 6 per group) were injected "
          "subcutaneously with 5 x 10^6 U251 cells. When tumours reached about 100 mm^3, mice "
          "received DMC-GF (20 mg/kg, intraperitoneally, every other day for 21 days) alone "
          "or combined with temozolomide (50 mg/kg). Tumour volume was recorded every three "
          "days and all animal procedures were approved by the institutional committee."),

        P("§2.8¶1", "§2.8", 3,
          "All data are presented as the mean ± SD of at least three independent experiments. "
          "Differences among groups were assessed by one-way ANOVA followed by Tukey's post "
          "hoc test in GraphPad Prism 9. p < 0.05 was considered statistically significant."),

        # ── Results：引用格式集中在这里 ──────────────────────────────────
        P("§3.1¶1", "§3.1", 4,
          "As shown in Fig. 1A, DMC-GF reduced the viability of U251 and U87 cells in a dose- "
          "and time-dependent manner, with IC50 values of 5.148, 3.205 and 2.677 μM at 12, 24 "
          "and 48 h respectively."),

        P("§3.1¶2", "§3.1", 4,
          "Apoptosis was then quantified by flow cytometry (Figure 1(b)); the apoptotic rate "
          "rose from 4.2% in the vehicle group to 31.7% at 3.0 μM. Quantification is shown in "
          "Fig. 1c. The onset was rapid: Fig. 1, 2 h after exposure the viability of treated "
          "wells had already declined."),

        P("§3.1¶3", "§3.1", 4,
          "Consistent results were obtained in the second cell line (see Fig 1 and Table 1). "
          "All fitted IC50 values are listed in Tab. 1."),

        P("§3.2¶1", "§3.2", 5,
          "DMC-GF markedly decreased GPX4 and SLC7A11 protein levels while HMOX1 was "
          "up-regulated (Fig. 2a-c). Together, Figs. 1 and 2 indicate that DMC-GF kills "
          "glioma cells at least partly through ferroptosis."),

        P("§3.2¶2", "§3.2", 5,
          "Intracellular ROS measured by DCFH-DA increased 3.4-fold, MDA accumulated and GSH "
          "was depleted (FIGURE 2). Pre-treatment with Fer-1 abolished all of these changes, "
          "which is summarised in figure 3."),

        P("§3.2¶3", "§3.2", 6,
          "Figures 1-3 together support the proposed mechanism, which is also consistent with "
          "Supplementary Fig. S1."),

        P("§4¶1", "§4", 7,
          "In summary (Fig.1 and Fig. 2), DMC-GF promotes ferroptosis and sensitises glioma "
          "to temozolomide. The vehicle design of Figure~1 also rules out a solvent effect."),
    ]

    assets = [
        FigureAsset(
            figure_id="Figure 1", kind="figure", number="1",
            caption=(
                "Fig. 1 DMC-GF exhibits pronounced antitumour activity in vitro. "
                "(A) CCK-8 assay of cell viability in U251 and U87 cells treated with "
                "0-4.5 μM DMC-GF for 12, 24 and 48 h. "
                "(B) Representative flow cytometry plots of Annexin V/propidium iodide "
                "staining after treatment with 0, 1.5 or 3.0 μM DMC-GF for 24 h. "
                "(C) Quantification of the apoptotic rate. (A) 30%."
            ),
            pages=[4], images=["figures/fig_01.png"],
            layout_route=LayoutRoute.JOURNAL_RASTER, bbox_confidence=Confidence.HIGH,
        ),
        FigureAsset(
            figure_id="Figure 2", kind="figure", number="2",
            caption=(
                "Figure 2. DMC-GF triggers ferroptosis in glioma cells. "
                "(a) Western blot of GPX4, SLC7A11 and HMOX1 in U251 cells treated with "
                "3.0 μM DMC-GF for 24 h. "
                "(b) Intracellular ROS measured by DCFH-DA; ROS increased (A) 30% relative "
                "to the vehicle control. "
                "(c) MDA content and GSH levels in U251 and U87 cells; n = 3 biological "
                "replicates, one-way ANOVA."
            ),
            pages=[5], images=["figures/fig_02.png"],
            layout_route=LayoutRoute.JOURNAL_RASTER, bbox_confidence=Confidence.HIGH,
        ),
        FigureAsset(
            figure_id="Figure 3", kind="figure", number="3",
            caption=(
                "Fig. 3 Proposed model. DMC-GF stabilises HMOX1 downstream of Nrf2, driving "
                "lipid peroxidation and ferroptosis, thereby increasing the susceptibility of "
                "glioma to temozolomide."
            ),
            pages=[6, 7], images=["figures/fig_03.png", "figures/fig_03b.png"],
            layout_route=LayoutRoute.FALLBACK_BAND, bbox_confidence=Confidence.MEDIUM,
            warnings=["图题起始于下一页顶部，按三级回退配对"],
        ),
        FigureAsset(
            figure_id="Table 1", kind="table", number="1",
            caption=(
                "Table 1 IC50 values (μM) of DMC-GF and temozolomide in glioma cell lines "
                "after 24 h of treatment."
            ),
            pages=[4], images=["figures/tab_01.png"],
            layout_route=LayoutRoute.FALLBACK_BAND, bbox_confidence=Confidence.MEDIUM,
            table_text_grid=[
                ["Cell line", "DMC-GF IC50 (μM)", "TMZ IC50 (μM)", "Combination index"],
                ["U251", "3.205", "198.4", "0.62"],
                ["U87", "3.874", "221.7", "0.71"],
            ],
        ),
    ]

    return ParsedPaper(
        pdf_path="synthetic_bio.pdf",
        title="The curcumin derivative DMC-GF promotes ferroptosis in glioma",
        n_pages=8, sections=sections, paragraphs=paragraphs, assets=assets,
    )


# ══════════════════════════════════════════════════════════════════════════
# 合成论文 B · ML/NLP
# ══════════════════════════════════════════════════════════════════════════


def make_ml_paper() -> ParsedPaper:
    sections = [
        Section(id="§1", title="Introduction", level=1, page_start=1),
        Section(id="§2", title="Experimental Setup", level=1, page_start=2, is_methods=True),
        Section(id="§2.1", title="Models", level=2, page_start=2, is_methods=True),
        Section(id="§2.2", title="Datasets and Metrics", level=2, page_start=2, is_methods=True),
        Section(id="§2.3", title="Training Details", level=2, page_start=3, is_methods=True),
        Section(id="§3", title="Results", level=1, page_start=3),
        Section(id="§4", title="Ablation Analysis", level=1, page_start=4),
    ]
    paragraphs = [
        Paragraph(id="§2.1¶1", section_id="§2.1", page=2, text=(
            "We evaluate three open multilingual language models: BLOOM-7B1, XGLM-7.5B and "
            "Llama-2-7B. All checkpoints are taken from the HuggingFace Hub and run in "
            "bfloat16 on a single A100 GPU. We additionally include mT5-large as an "
            "encoder-decoder reference.")),
        Paragraph(id="§2.2¶1", section_id="§2.2", page=2, text=(
            "Translation quality is measured on FLORES-200 with spBLEU and chrF++, while "
            "cross-lingual understanding is evaluated on XNLI (accuracy) and MGSM (exact "
            "match). Every number is the mean over 3 random seeds; error bars denote the "
            "standard deviation across seeds.")),
        Paragraph(id="§2.3¶1", section_id="§2.3", page=3, text=(
            "We fine-tune with LoRA (rank 16, alpha 32) for 3 epochs using AdamW, a learning "
            "rate of 2e-5, a batch size of 32 and a linear warmup over the first 10% of "
            "steps. Decoding uses beam search with beam size 5 and a maximum length of 256 "
            "tokens.")),
        Paragraph(id="§3¶1", section_id="§3", page=3, text=(
            "Figure 1 reports spBLEU on FLORES-200 for all three models. Fig. 1a shows the "
            "high-resource directions, whereas Fig. 1b covers the low-resource ones, where "
            "Llama-2-7B degrades most sharply.")),
        Paragraph(id="§3¶2", section_id="§3", page=3, text=(
            "Table II lists XNLI accuracy per language. The gap between BLOOM-7B1 and "
            "XGLM-7.5B narrows to 1.2 points once the prompt is translated.")),
        Paragraph(id="§4¶1", section_id="§4", page=4, text=(
            "Figs. 2 and 3 present the ablation over LoRA rank and beam size. Removing "
            "instruction tuning costs 4.1 spBLEU on average (Fig. 2, 3).")),
    ]
    assets = [
        FigureAsset(
            figure_id="Figure 1", kind="figure", number="1",
            caption=(
                "Figure 1: spBLEU on FLORES-200 for BLOOM-7B1, XGLM-7.5B and Llama-2-7B. "
                "(a) High-resource directions (en-de, en-fr). "
                "(b) Low-resource directions (en-sw, en-yo); error bars denote the standard "
                "deviation over 3 random seeds."
            ),
            pages=[3], images=["figures/fig_01.png"],
            layout_route=LayoutRoute.LATEX_VECTOR, bbox_confidence=Confidence.HIGH,
        ),
        FigureAsset(
            figure_id="Figure 2", kind="figure", number="2",
            caption="Figure 2: Ablation over the LoRA rank. Mean spBLEU across 3 seeds.",
            pages=[4], images=["figures/fig_02.png"],
            layout_route=LayoutRoute.LATEX_VECTOR, bbox_confidence=Confidence.HIGH,
        ),
        FigureAsset(
            figure_id="Figure 3", kind="figure", number="3",
            caption="Figure 3: Effect of beam size on spBLEU and decoding latency.",
            pages=[4], images=["figures/fig_03.png"],
            layout_route=LayoutRoute.LATEX_VECTOR, bbox_confidence=Confidence.HIGH,
        ),
        FigureAsset(
            figure_id="Table II", kind="table", number="II",
            caption="Table II: XNLI accuracy (%) for 15 languages. Best result per row in bold.",
            pages=[3], images=["figures/tab_02.png"],
            layout_route=LayoutRoute.FALLBACK_BAND, bbox_confidence=Confidence.LOW,
            table_text_grid=[
                ["Language", "BLOOM-7B1", "XGLM-7.5B", "Llama-2-7B"],
                ["en", "54.2", "55.1", "58.7"],
                ["sw", "41.3", "44.0", "35.2"],
            ],
        ),
    ]
    return ParsedPaper(
        pdf_path="synthetic_ml.pdf",
        title="How multilingual are open LLMs? A controlled study",
        n_pages=6, sections=sections, paragraphs=paragraphs, assets=assets,
    )


# ══════════════════════════════════════════════════════════════════════════
# 合成论文 C · 中文
# ══════════════════════════════════════════════════════════════════════════


def make_cn_paper() -> ParsedPaper:
    sections = [
        Section(id="§1", title="引言", level=1, page_start=1),
        Section(id="§2", title="材料与方法", level=1, page_start=2, is_methods=True),
        Section(id="§3", title="结果", level=1, page_start=3),
    ]
    paragraphs = [
        Paragraph(id="§2¶1", section_id="§2", page=2, text=(
            "细胞培养与药物处理：U251 细胞培养于含 10% 胎牛血清的 DMEM 培养基中，"
            "置于 37 °C、5% CO2 培养箱。采用 CCK-8 法检测细胞活力，"
            "每组设 6 个复孔，独立重复 3 次。统计学分析采用单因素方差分析，p < 0.05 为差异有统计学意义。")),
        Paragraph(id="§3¶1", section_id="§3", page=3, text=(
            "如图 1 所示，DMC-GF 以剂量依赖方式抑制 U251 细胞增殖；"
            "图1A 为 12 h 的结果，图 1、2 显示 24 h 与 48 h 的差异更为明显。")),
        Paragraph(id="§3¶2", section_id="§3", page=3, text=(
            "表 2 列出了各组的 IC50 值。需要说明的是，本文的列表 3 个指标均取自同一批实验，"
            "处理时间为 24 h。")),
        Paragraph(id="§3¶3", section_id="§3", page=4, text="详见图3 与表 2。"),
    ]
    assets = [
        FigureAsset(
            figure_id="Figure 1", kind="figure", number="1",
            caption="图 1 DMC-GF 抑制胶质瘤细胞增殖。(A) CCK-8 法检测细胞活力。(B) 克隆形成实验。",
            pages=[3], images=["figures/fig_01.png"],
            layout_route=LayoutRoute.JOURNAL_RASTER, bbox_confidence=Confidence.HIGH,
        ),
        FigureAsset(
            figure_id="Figure 2", kind="figure", number="2",
            caption="图 2 不同处理时间下的细胞活力曲线。",
            pages=[3], images=["figures/fig_02.png"],
            layout_route=LayoutRoute.JOURNAL_RASTER, bbox_confidence=Confidence.HIGH,
        ),
        FigureAsset(
            figure_id="Figure 3", kind="figure", number="3",
            caption="图 3 作用机制示意图。",
            pages=[4], images=["figures/fig_03.png"],
            layout_route=LayoutRoute.JOURNAL_RASTER, bbox_confidence=Confidence.HIGH,
        ),
        FigureAsset(
            figure_id="Table 2", kind="table", number="2",
            caption="表 2 各细胞株的 IC50 值。",
            pages=[3], images=["figures/tab_02.png"],
            layout_route=LayoutRoute.FALLBACK_BAND, bbox_confidence=Confidence.MEDIUM,
            table_text_grid=[["细胞株", "IC50 (μM)"], ["U251", "3.205"], ["U87", "3.874"]],
        ),
    ]
    return ParsedPaper(
        pdf_path="synthetic_cn.pdf", title="姜黄素衍生物 DMC-GF 对胶质瘤细胞的作用",
        n_pages=5, sections=sections, paragraphs=paragraphs, assets=assets,
    )


# ══════════════════════════════════════════════════════════════════════════
# 单元测试 1 · 交叉引用正则
# ══════════════════════════════════════════════════════════════════════════

REF_CASES: list[tuple[str, list[tuple[str, str]]]] = [
    # ── 基本变体 ────────────────────────────────────────────────
    ("As shown in Fig. 1, the effect is clear.", [("figure", "1")]),
    ("Fig 1 shows the workflow.", [("figure", "1")]),
    ("Figure 1 shows the workflow.", [("figure", "1")]),
    ("see FIGURE 1 for details", [("figure", "1")]),
    ("see figure 1 for details", [("figure", "1")]),
    ("as illustrated (Fig.1)", [("figure", "1")]),
    ("LaTeX writes Figure~1 like this", [("figure", "1")]),
    ("with a non-breaking space Fig. 12 here", [("figure", "12")]),
    # ── 带子图标号 ──────────────────────────────────────────────
    ("in Fig. 1A the curve drops", [("figure", "1")]),
    ("in Figure 1(a) the curve drops", [("figure", "1")]),
    ("in Fig. 1a-c the curves drop", [("figure", "1")]),
    ("in Fig. 5a-d we see", [("figure", "5")]),
    ("in Fig. 1 (a-c) we see", [("figure", "1")]),
    # ── 复数与范围 ──────────────────────────────────────────────
    ("Figs. 1 and 2 show", [("figure", "1"), ("figure", "2")]),
    ("Figures 1-3 show", [("figure", "1"), ("figure", "2"), ("figure", "3")]),
    ("Fig. 1, 2 show", [("figure", "1"), ("figure", "2")]),
    ("as in Figure 2, 3 and 4", [("figure", "2"), ("figure", "3"), ("figure", "4")]),
    ("Figs. 2 and 3 present the ablation", [("figure", "2"), ("figure", "3")]),
    # ── 表格 ────────────────────────────────────────────────────
    ("Table 1 lists the values", [("table", "1")]),
    ("Tab. 3 lists the values", [("table", "3")]),
    ("TABLE 2 lists the values", [("table", "2")]),
    ("Table IV lists the values", [("table", "4")]),
    ("Tables 1 and 2 list the values", [("table", "1"), ("table", "2")]),
    ("see Fig. 1 and Table 1", [("figure", "1"), ("table", "1")]),
    # ── 补充材料 ────────────────────────────────────────────────
    ("consistent with Supplementary Fig. S1", [("figure", "S1")]),
    ("see Supplementary Table S2", [("table", "S2")]),
    # ── 中文 ────────────────────────────────────────────────────
    ("如图 1 所示", [("figure", "1")]),
    ("图1A 为 12 h 的结果", [("figure", "1")]),
    ("图 1、2 显示差异", [("figure", "1"), ("figure", "2")]),
    ("表 2 列出了 IC50 值", [("table", "2")]),
    ("详见图3 与表 2。", [("figure", "3"), ("table", "2")]),
    # ── 陷阱：不能命中 ──────────────────────────────────────────
    ("The onset was rapid: Fig. 1, 2 h after exposure", [("figure", "1")]),
    ("Fig. 1 and 2 μM was enough", [("figure", "1")]),
    ("本文的列表 3 个指标均取自同一批实验", []),
    ("mice were treated for 2 h and 3 days", []),
    ("we configure the table below with 3 columns", []),
    ("the IC50 was 3.205 μM in 24 h", []),
]


def test_refs() -> None:
    print("\n[1] 交叉引用正则")
    for text, expected in REF_CASES:
        got = [(r.kind, r.number) for r in ctx.find_figure_refs(text)]
        eq(got, expected, f"引用扫描：{text!r}")

    # 子图标号解析
    eq([r.panels for r in ctx.find_figure_refs("in Fig. 1A")], [["A"]], "子图 Fig. 1A")
    eq([r.panels for r in ctx.find_figure_refs("in Figure 1(a)")], [["A"]], "子图 Figure 1(a)")
    eq([r.panels for r in ctx.find_figure_refs("in Fig. 1a-c")], [["A", "B", "C"]], "子图范围 1a-c")
    eq([r.panels for r in ctx.find_figure_refs("in Fig. 1 (a-c)")], [["A", "B", "C"]],
       "子图范围 1 (a-c)")
    eq([r.panels for r in ctx.find_figure_refs("treated for Fig. 1 3 d")], [[]],
       "`Fig. 1 3 d` 的 d 是天数不是子图")
    eq(ctx.find_figure_refs("in Fig. 1d")[0].panels, ["D"], "`Fig. 1d` 紧贴数字才算子图 d")

    # 归一化
    eq(ctx.norm_number("01"), "1", "图号归一化：补零")
    eq(ctx.norm_number("IV"), "4", "图号归一化：罗马数字")
    eq(ctx.norm_number("S 2"), "S2", "图号归一化：补充材料")


# ══════════════════════════════════════════════════════════════════════════
# 单元测试 2 · 子图切分
# ══════════════════════════════════════════════════════════════════════════


def test_panels() -> None:
    print("\n[2] 子图描述切分")

    p, lead, _ = ctx.split_panel_captions(
        "Fig. 1 DMC-GF exhibits antitumour activity. (A) CCK-8 assay of cell viability in "
        "U251 cells. (B) Flow cytometry of apoptosis. (C) Quantification of apoptotic rate."
    )
    eq(sorted(p), ["A", "B", "C"], "(A)(B)(C) 切分")
    check("CCK-8" in p.get("A", ""), "panel A 描述含 CCK-8")
    eq(lead, "DMC-GF exhibits antitumour activity", "整图总述")

    p, _, _ = ctx.split_panel_captions(
        "Figure 2: Ablation study. (a) Effect of the LoRA rank on spBLEU. "
        "(b) Effect of beam size on decoding latency."
    )
    eq(sorted(p), ["A", "B"], "(a)(b) 小写标号归一到大写")

    p, _, _ = ctx.split_panel_captions(
        "Fig. 4 Survival analysis. A. Kaplan-Meier curve of the vehicle group. "
        "B. Kaplan-Meier curve of the treated group."
    )
    eq(sorted(p), ["A", "B"], "`A. ... B. ...` 裸标号切分")

    p, _, _ = ctx.split_panel_captions(
        "Fig. 5 Tumour growth. A) Tumour volume over time. B) Final tumour weight."
    )
    eq(sorted(p), ["A", "B"], "`A) ... B) ...` 切分")

    p, _, _ = ctx.split_panel_captions(
        "Fig. 6 Western blot. (A-C) GPX4, SLC7A11 and HMOX1 protein levels in U251 cells. "
        "(D) Densitometric quantification of the three blots."
    )
    eq(sorted(p), ["A", "B", "C", "D"], "`(A-C)` 范围标号展开")
    eq(p["A"], p["C"], "范围标号共享同一段描述")

    p, _, _ = ctx.split_panel_captions(
        "Figure 3: Attention maps. (a) Layer 1 of the encoder stack, "
        "(b) layer 12 of the encoder stack."
    )
    eq(sorted(p), ["A", "B"], "逗号分隔的 `(a) ..., (b) ...` 也要切开")

    # ── 误切陷阱 ────────────────────────────────────────────────
    p, _, _ = ctx.split_panel_captions(
        "Fig. 7 ROS levels. Intracellular ROS increased (A) 30% relative to the control."
    )
    eq(p, {}, "句中的 `(A)` 不是子图标号")

    p, _, _ = ctx.split_panel_captions("Fig. 8 Summary of the results. (A) 30%. (B) 40%.")
    eq(p, {}, "标号后描述过短，不算子图")

    p, _, _ = ctx.split_panel_captions(
        "Figure 9: Overview of our framework. The encoder is shared across languages."
    )
    eq(p, {}, "没有标号的图题返回空")

    p, _, _ = ctx.split_panel_captions(
        "Fig. 10 Cells were treated as described in (C) of the previous figure, "
        "with the same conditions applied throughout the whole experiment."
    )
    eq(p, {}, "孤立的 `(C)` 不成链，判为引用而非标号")

    p, lead, _ = ctx.split_panel_captions(
        "图 1 DMC-GF 抑制胶质瘤细胞增殖。(A) CCK-8 法检测细胞活力。(B) 克隆形成实验。"
    )
    eq(sorted(p), ["A", "B"], "中文图题子图切分")
    eq(lead, "DMC-GF 抑制胶质瘤细胞增殖", "中文整图总述")


# ══════════════════════════════════════════════════════════════════════════
# 单元测试 2b · 真实图题回归（端到端验收暴露的三个缺陷）
#   前两条来自 Scientific Reports（scirep_dmcgf.pdf），后两条来自 ACL（acl_shifcon.pdf），
#   都是 L1 抽出的**原文**，一字未改。切分规则的边界由它们锁定。
# ══════════════════════════════════════════════════════════════════════════

REAL_FIG5 = (
    "Fig. 5. Effects of TMZ and TMZ combined with DMC-GF on tumor growth in an orthotopic "
    "glioma model. (A) BLI monitoring of xenograft glioma tumor growth (B) Measure the size "
    "of heterotopic in situ glioblastoma using the IVIS spectral imaging system."
)

REAL_FIG4 = (
    "Fig. 4. DMC-GF enhances HMOX1 expression in glioma cells via Keap1/Nrf2. All "
    "representative images for each group were acquired under identical fixed exposure "
    "parameters to ensure comparability. The quantitative results in panel B represent the "
    "mean nuclear/cytoplasmic ratio from multiple fields (> 50 cells per group), reflecting "
    "the degree of NRF2 activation independent of variations in total protein levels. "
    "(A) Representative images of NRF2 immunofluorescence staining. Green: NRF2; Blue: DAPI "
    "(cell nuclei) Scale bar: 20 μm (B) Quantitative analysis of NRF2 nuclear-to-cytoplasmic "
    "ratio. Quantified by measuring the average NRF2 fluorescence intensity in both nuclear "
    "and cytoplasmic regions per cell and calculating their ratio (nuclear/ cytoplasmic). "
    "(C) Effect of DMC-GF on NRF2 and HMOX1 protein expression. Representative Western blot "
    "images of NRF2, HMOX1, and Tubulin (loading control) in glioma cells treated with DMC-GF "
    "at the indicated concentrations. (D) Quantification of NRF2 and HMOX1 protein "
    "expression.* indicates p < 0.05, ** indicates p < 0.01, *** indicates p < 0.001. N = 3. "
    "The control group was used for comparison. Data are expressed as mean ± SD."
)

REAL_FIG3 = (
    "Fig. 3. DMC-GF enhances ferroptosis in glioblastoma by upregulating HMOX1. "
    "(A) Representative fluorescence microscopy images of U87 cells stained with the "
    "C11-BODIPY 581/591 probe following indicated treatments. Green fluorescence indicates "
    "the oxidized state (lipid peroxidation), while red fluorescence indicates the reduced "
    "state. Scale bar: 20 μm. (B) Quantitative analysis of the oxidized (green) fluorescence "
    "intensity from (A). Data are presented as mean ± SD (n = 3 independent experiments). "
    "(C) Representative flow cytometry scatter plots of U87 cells stained with C11-BODIPY "
    "581/591. The vertical axis (FITC-H) represents the oxidized state (green fluorescence), "
    "and the horizontal axis (PE-Texas Red-H) represents the reduced state (red "
    "fluorescence). (D) Quantitative flow cytometric analysis of the mean fluorescence "
    "intensity (MFI) of the oxidized C11-BODIPY signal. * indicates p < 0.05, ** indicates "
    "p < 0.01, *** indicates p < 0.001. N = 3. The control group was used for comparison. "
    "Data are expressed as mean ± SD."
)

# ACL Fig. 1：图题用散文指代子图（`Projection (a) shows ...`），但**切图里确实画着
# (a) (b) 两个虚线框子图**，所以必须切。图题文法不是真相，图才是。
# 描述要从限定名词 `Projection` 起算，否则切出「shows the representations ...」这种残句。
# 另外 `imply- ing` 是 PDF 断词残留，装配时要修回 `implying`。
REAL_ACL_FIG1 = (
    "Figure 1: Two different projections on the sentence representations visualized using "
    "LDA. Projection (a) shows the representations are mutually aligned, imply- ing a "
    "language-agnostic status, whereas projection (b) illustrates separated representations "
    "in distinct spaces, suggesting a language-specific status. The sentence representations "
    "are obtained through mean-pooling the hidden states from the 15th layer of Llama-27B."
)

# ACL Fig. 7：`(a) shows ...` 落在句号之后，描述以动词开头，要把标号补回去当主语。
REAL_ACL_FIG7 = (
    "Figure 7: The low subspace distance areas of different models are delineated with "
    "dashed boxes. (a) shows the results for different model families; (b) shows the results "
    "for different scales of XGLM."
)


def test_real_captions() -> None:
    print("\n[2b] 真实图题回归")

    # ── 缺陷 1：`(B)` 前没有句末标点 ────────────────────────────
    p, pre, tail = ctx.split_panel_captions(REAL_FIG5)
    eq(sorted(p), ["A", "B"], "真实 Fig. 5：`... tumor growth (B) Measure ...` 无句读也要切开")
    check(p["A"].startswith("BLI monitoring"), f"Fig. 5 panel A 描述（实得 {p.get('A')!r}）")
    check(p["B"].startswith("Measure the size"), f"Fig. 5 panel B 描述（实得 {p.get('B')!r}）")
    check(pre.endswith("orthotopic glioma model"), f"Fig. 5 前置总述（实得 {pre!r}）")
    eq(tail, "", "Fig. 5 没有收尾说明")

    # ── 缺陷 2：前置散文提到 panel B + 各种非标号括号 ───────────
    p, pre, tail = ctx.split_panel_captions(REAL_FIG4)
    eq(sorted(p), ["A", "B", "C", "D"], "真实 Fig. 4：A~D 全切出（前置散文里的 `panel B` 不算标号）")
    check("immunofluorescence" in p["A"], "Fig. 4 panel A 是免疫荧光代表性图")
    check(p["B"].startswith("Quantitative analysis"), f"Fig. 4 panel B（实得 {p['B'][:40]!r}）")
    check("Western blot" in p["C"], "Fig. 4 panel C 是 WB")
    check("panel B represent" in pre, "提到 panel B 的散文归入前置总述")
    check("(> 50 cells per group)" in pre, "`(> 50 cells per group)` 不是标号")

    # ── 缺陷 3：全图共享收尾必须与末尾 panel 分离 ───────────────
    check("N = 3" in tail, f"Fig. 4：`N = 3` 落在 caption_trailer（实得 {tail!r}）")
    check("N = 3" not in p["D"], "Fig. 4：`N = 3` 不能留在 panel D 里")
    check("mean ± SD" in tail, "Fig. 4：`mean ± SD` 落在 caption_trailer")
    check("mean ± SD" not in p["D"], "Fig. 4：`mean ± SD` 不能留在 panel D 里")
    check("* indicates p < 0.05" in tail, "Fig. 4：显著性定义落在 caption_trailer")
    eq(p["D"], "Quantification of NRF2 and HMOX1 protein expression.",
       "Fig. 4 panel D 只保留自己的描述")

    # ── Fig. 3：panel B 里的 `from (A)` 是引用；中段的 n=3 属于 panel B 不能上提 ──
    p, _, tail = ctx.split_panel_captions(REAL_FIG3)
    eq(sorted(p), ["A", "B", "C", "D"], "真实 Fig. 3：panel B 描述里的 `from (A)` 不破坏链")
    check("(n = 3 independent experiments)" in p["B"],
          "Fig. 3：panel B 自己的 n 留在 panel B（它不在末尾，不算全图收尾）")
    check("N = 3" in tail and "mean ± SD" in tail, "Fig. 3：末尾的全图收尾正确切出")
    check("(FITC-H)" in p["C"] and "(MFI)" in p["D"], "缩写括号不被当成标号")

    # ── ACL：散文式标号也要切，且描述不能是残句 ─────────────────
    p, _, _ = ctx.split_panel_captions(REAL_ACL_FIG1)
    eq(sorted(p), ["A", "B"], "真实 ACL Fig. 1：图里确有 (a)(b) 两个子图，必须切")
    check(p["A"].startswith("Projection (a) shows"),
          f"ACL Fig. 1 panel A 要带上限定名词 Projection（实得 {p.get('A')!r}）")
    check(p["B"].startswith("projection (b) illustrates"),
          f"ACL Fig. 1 panel B 同理（实得 {p.get('B')!r}）")
    check("implying" in p["A"] and "imply- ing" not in p["A"],
          f"ACL Fig. 1：PDF 断词 `imply- ing` 修回 `implying`（实得 {p.get('A')!r}）")
    check(not p["A"].rstrip().endswith("whereas"), "段末悬着的连词 `whereas` 要去掉")

    p, _, _ = ctx.split_panel_captions(REAL_ACL_FIG7)
    eq(sorted(p), ["A", "B"], "真实 ACL Fig. 7：句号后的 `(a) shows ...` 是标号")
    check(p["A"].startswith("(a) shows the results"),
          f"ACL Fig. 7 panel A：描述以动词开头时把标号补回当主语（实得 {p.get('A')!r}）")

    # ── 断词修复 ────────────────────────────────────────────────
    eq(ctx._dehyphenate("imply- ing a language-agnostic status"),
       "implying a language-agnostic status", "换行断词：去连字符")
    eq(ctx._dehyphenate("Protein- protein interaction (PPI) network"),
       "Protein-protein interaction (PPI) network", "排版空格：保留连字符")
    eq(ctx._dehyphenate("informa- tion retrieval"), "information retrieval", "断词 -tion")
    eq(ctx._dehyphenate("dose-response curve"), "dose-response curve", "正常连字符不动")
    eq(ctx._dehyphenate("treated with 20 mg/kg - intraperitoneally"),
       "treated with 20 mg/kg - intraperitoneally", "破折号不动")


# ══════════════════════════════════════════════════════════════════════════
# 单元测试 3 · 文件名规范化
# ══════════════════════════════════════════════════════════════════════════


def test_stems() -> None:
    print("\n[3] figure_id → 文件名")

    def stem(fid: str, kind: str, num: str) -> str:
        return ctx.asset_stem(FigureAsset(
            figure_id=fid, kind=kind, number=num, caption="", pages=[1], images=[],
            layout_route=LayoutRoute.JOURNAL_RASTER, bbox_confidence=Confidence.HIGH,
        ))

    eq(stem("Figure 1", "figure", "1"), "fig_01", "Figure 1 → fig_01")
    eq(stem("Figure 12", "figure", "12"), "fig_12", "Figure 12 → fig_12")
    eq(stem("Table 3", "table", "3"), "tab_03", "Table 3 → tab_03")
    eq(stem("Table IV", "table", "IV"), "tab_04", "Table IV → tab_04")
    eq(stem("Figure S2", "figure", "S2"), "fig_s02", "Figure S2 → fig_s02")


# ══════════════════════════════════════════════════════════════════════════
# 端到端
# ══════════════════════════════════════════════════════════════════════════


def rank(pack, para_id: str) -> int:
    ids = [p.id for p in pack.methods_paragraphs]
    return ids.index(para_id) if para_id in ids else 99


def test_bio(out: Path):
    print("\n[4] 端到端 · 生物医学论文")
    paper = make_bio_paper()
    packs = ctx.build_context_packs(paper, out)
    by_id = {p.figure_id: p for p in packs}
    eq(len(packs), 4, "4 个 asset → 4 个 pack")

    for name in ("fig_01.md", "fig_02.md", "fig_03.md", "tab_01.md"):
        check((out / "context" / name).is_file(), f"context/{name} 已写出")

    f1, f2, t1 = by_id["Figure 1"], by_id["Figure 2"], by_id["Table 1"]

    eq([p.id for p in f1.citing_paragraphs],
       ["§3.1¶1", "§3.1¶2", "§3.1¶3", "§3.2¶1", "§3.2¶3", "§4¶1"],
       "Figure 1 的引用段（Fig. 1A / Figure 1(b) / Fig 1 / Figs. 1 and 2 / Figures 1-3 / Fig.1）")

    eq([p.id for p in f2.citing_paragraphs], ["§3.2¶1", "§3.2¶2", "§3.2¶3", "§4¶1"],
       "Figure 2 的引用段（不含 §3.1¶2 —— 那里的 `Fig. 1, 2 h` 是时长陷阱）")
    check("§3.1¶2" not in [p.id for p in f2.citing_paragraphs],
          "时长陷阱 `Fig. 1, 2 h` 没有被误判成 Figure 2")

    eq([p.id for p in t1.citing_paragraphs], ["§3.1¶3"], "Table 1 的引用段（Table 1 / Tab. 1）")
    eq(t1.table_text_grid, paper.assets[3].table_text_grid, "table_text_grid 原样透传")
    eq(t1.kind, "table", "表格 pack 的 kind")

    eq(sorted(f1.panel_captions), ["A", "B", "C"], "Figure 1 子图切分")
    check("CCK-8" in f1.panel_captions["A"], "Figure 1 panel A 描述含 CCK-8")
    check("Annexin V" in f1.panel_captions["B"], "Figure 1 panel B 描述含 Annexin V")
    eq(sorted(f2.panel_captions), ["A", "B", "C"], "Figure 2 子图切分（小写 + 句中 (A) 陷阱）")
    check("(A) 30%" in f2.panel_captions["B"],
          "句中的 `(A) 30%` 留在 panel b 描述里，没有被切成新 panel")

    m1 = [p.id for p in f1.methods_paragraphs]
    check("§2.2¶1" in m1, f"Figure 1 召回 CCK-8 方法段 §2.2¶1（实得 {m1}）")
    check("§2.3¶1" in m1, f"Figure 1 召回流式方法段 §2.3¶1（实得 {m1}）")
    check(rank(f1, "§2.2¶1") < rank(f1, "§2.7¶1"),
          "Figure 1：CCK-8 方法段排在异种移植方法段之前")
    check(rank(f1, "§2.2¶1") < rank(f1, "§2.6¶1"),
          "Figure 1：CCK-8 方法段排在 RNA-seq 方法段之前")

    m2 = [p.id for p in f2.methods_paragraphs]
    check("§2.4¶1" in m2, f"Figure 2 召回 western blot 方法段 §2.4¶1（实得 {m2}）")
    check("§2.5¶1" in m2, f"Figure 2 召回 ROS/MDA/GSH 方法段 §2.5¶1（实得 {m2}）")
    check(rank(f2, "§2.4¶1") < rank(f2, "§2.6¶1"),
          "Figure 2：western blot 方法段排在 RNA-seq 方法段之前")

    # 示意图：没有子图、Methods 靠兜底
    f3 = by_id["Figure 3"]
    eq(f3.panel_captions, {}, "示意图 Figure 3 无子图")
    check(len(f3.methods_paragraphs) > 0, "示意图仍给出兜底 Methods 段")
    eq(f3.bbox_confidence, Confidence.MEDIUM, "bbox_confidence 透传")
    eq(f3.images, ["figures/fig_03.png", "figures/fig_03b.png"], "跨页图两张切图都带上")

    md = (out / "context" / "fig_01.md").read_text(encoding="utf-8")
    check("§3.1¶1" in md, "md 里出现段落 id（供 evidence 溯源）")
    check("six replicate wells" in md, "md 里带上了 n（六复孔）—— 抽取时才不用脑补")
    check("one-way ANOVA" in md or "ANOVA" in md, "md 里带上了统计方法")
    check("CCK-8" in md, "md 里带上了 assay 名")
    check("装配诊断" in md, "md 含诊断小节")
    tmd = (out / "context" / "tab_01.md").read_text(encoding="utf-8")
    check("| U251 | 3.205 | 198.4 | 0.62 |" in tmd, "表格文本网格渲染成 markdown 表")
    return paper, packs


def test_ml(out: Path):
    print("\n[5] 端到端 · ML/NLP 论文")
    paper = make_ml_paper()
    packs = ctx.build_context_packs(paper, out)
    by_id = {p.figure_id: p for p in packs}
    f1, f2, f3, t2 = (by_id["Figure 1"], by_id["Figure 2"],
                      by_id["Figure 3"], by_id["Table II"])

    eq([p.id for p in f1.citing_paragraphs], ["§3¶1"], "Figure 1 引用段")
    eq([p.id for p in f2.citing_paragraphs], ["§4¶1"], "Figure 2 引用段（Figs. 2 and 3 / Fig. 2, 3）")
    eq([p.id for p in f3.citing_paragraphs], ["§4¶1"], "Figure 3 引用段")
    eq([p.id for p in t2.citing_paragraphs], ["§3¶2"], "Table II 引用段（罗马数字归一化到 2）")
    check((out / "context" / "tab_02.md").is_file(), "Table II → tab_02.md")

    eq(sorted(f1.panel_captions), ["A", "B"], "ACL 图题 (a)(b) 切分")
    m1 = [p.id for p in f1.methods_paragraphs]
    check("§2.1¶1" in m1, f"Figure 1 召回模型段 §2.1¶1（实得 {m1}）")
    check("§2.2¶1" in m1, f"Figure 1 召回数据集/指标段 §2.2¶1（实得 {m1}）")
    check(rank(f1, "§2.2¶1") < rank(f1, "§2.3¶1"),
          "Figure 1：数据集/指标段排在训练超参段之前")
    m2 = [p.id for p in f2.methods_paragraphs]
    check("§2.3¶1" in m2, f"Figure 2（LoRA rank 消融）召回训练细节段 §2.3¶1（实得 {m2}）")

    md = (out / "context" / "fig_01.md").read_text(encoding="utf-8")
    check("3 random seeds" in md, "md 里带上了 seeds 数（mlcs 的 n）")
    check("FLORES-200" in md and "spBLEU" in md, "md 里带上了数据集与指标")
    return paper, packs


def test_cn(out: Path):
    print("\n[6] 端到端 · 中文论文")
    paper = make_cn_paper()
    packs = ctx.build_context_packs(paper, out)
    by_id = {p.figure_id: p for p in packs}
    f1, f2, f3, t2 = (by_id["Figure 1"], by_id["Figure 2"],
                      by_id["Figure 3"], by_id["Table 2"])

    eq([p.id for p in f1.citing_paragraphs], ["§3¶1"], "图 1 引用段（如图 1 / 图1A / 图 1、2）")
    eq([p.id for p in f2.citing_paragraphs], ["§3¶1"], "图 2 引用段（图 1、2 的第二项）")
    eq([p.id for p in f3.citing_paragraphs], ["§3¶3"], "图 3 引用段（详见图3）")
    eq([p.id for p in t2.citing_paragraphs], ["§3¶2", "§3¶3"], "表 2 引用段")
    eq(sorted(f1.panel_captions), ["A", "B"], "中文图题子图切分")
    check([p.id for p in f1.methods_paragraphs] == ["§2¶1"],
          f"中文论文召回方法段（实得 {[p.id for p in f1.methods_paragraphs]}）")
    md = (out / "context" / "fig_01.md").read_text(encoding="utf-8")
    check("每组设 6 个复孔" in md, "中文 md 编码正确且带上了 n")
    return paper, packs


def test_robustness(out: Path) -> None:
    print("\n[7] 鲁棒性：单图装配失败不中断整体")
    paper = make_bio_paper()
    original = ctx.split_panel_captions

    def boom(caption: str):
        if "triggers ferroptosis" in caption:  # 只让 Figure 2 炸
            raise ValueError("模拟切分崩溃")
        return original(caption)

    ctx.split_panel_captions = boom  # type: ignore[assignment]
    try:
        packs = ctx.build_context_packs(paper, out / "_broken")
    finally:
        ctx.split_panel_captions = original  # type: ignore[assignment]

    eq(len(packs), 4, "一图失败，其余照常产出 4 个 pack")
    by_id = {p.figure_id: p for p in packs}
    eq(by_id["Figure 2"].panel_captions, {}, "失败图退化为空子图")
    eq(by_id["Figure 2"].caption, paper.assets[1].caption, "失败图仍保留图题")
    check(len(by_id["Figure 1"].citing_paragraphs) == 6, "其它图不受影响")
    md = (out / "_broken" / "context" / "fig_02.md").read_text(encoding="utf-8")
    check("装配失败" in md, "失败原因写进了 md 告警")


def main() -> int:
    quiet = "--quiet" in sys.argv
    if OUT.exists():
        shutil.rmtree(OUT)

    test_refs()
    test_panels()
    test_real_captions()
    test_stems()
    test_bio(OUT / "bio")
    test_ml(OUT / "ml")
    test_cn(OUT / "cn")
    test_robustness(OUT / "bio")

    print("\n" + "=" * 74)
    if _failures:
        print(f"FAILED  {len(_failures)}/{_checks} 项断言未通过")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"PASSED  {_checks}/{_checks} 项断言全部通过")
    print(f"产物目录：{OUT}")

    if not quiet:
        for rel in ("bio/context/fig_01.md", "bio/context/tab_01.md",
                    "ml/context/fig_01.md", "cn/context/fig_01.md"):
            print("\n" + "=" * 74)
            print(f"===== {rel} " + "=" * (max(0, 60 - len(rel))))
            print("=" * 74)
            print((OUT / rel).read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
