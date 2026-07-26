"""共享数据契约 —— 全流水线唯一真相源。

所有模块（L1 解析 / L2 装配 / L3 抽取 / L4 渲染）都从这里导入类型，
禁止各自另立结构。改动本文件即改动全局契约，需同步通知各层。

层间数据流：
    PDF ──L1──> ParsedPaper ──L2──> ContextPack[] ──L3──> FigureRecord[] ──L4──> 笔记/导图/表格
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ── 全局常量 ────────────────────────────────────────────────────────────────

#: 原文未交代时必须填这个值。禁止让模型编造，见 PLAN.md「两条铁律」。
NOT_REPORTED = "not_reported"

SCHEMA_VERSION = "1.0"


# ── L1 解析层输出 ───────────────────────────────────────────────────────────

BBox = tuple[float, float, float, float]  # (x0, y0, x1, y1)，PDF point 坐标



class LayoutRoute(str, Enum):
    """切图走了哪条路线，用于排查与置信度评估。"""

    JOURNAL_RASTER = "journal_raster"  # 期刊排版：单张大位图，直接取 image rect
    LATEX_VECTOR = "latex_vector"  # LaTeX 排版：矢量聚类 + 分栏 + ceiling
    FALLBACK_BAND = "fallback_band"  # 降级：切整栏带
    FALLBACK_PAGE = "fallback_page"  # 降级：切整页


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Paragraph(BaseModel):
    """正文段落。id 形如 `§3.2¶2`，供 evidence 溯源引用。"""

    id: str
    section_id: str
    page: int
    text: str


class Section(BaseModel):
    """章节树节点。id 形如 `§3.2`；level 1 为顶级章节。"""

    id: str
    title: str
    level: int
    page_start: int
    is_methods: bool = False  # Methods / Experimental Setup / 方法 等，供 L2 召回


class CaptionAnchor(BaseModel):
    """图题/表题锚点。是 L1 内部中间产物，也用于排查配对错误。"""

    kind: Literal["figure", "table"]
    number: str  # "1" / "2" / "IV"
    #: 图序列。空串 = 正文图；`extended data` / `supplementary` / `appendix` 各自独立。
    #: **图号只在同一序列内唯一**——Nature 系的 `Extended Data Fig. 1` 与正文 `Fig. 1`
    #: 是两张毫不相干的图，不区分序列就会被按图号合并成一条，导致切图张冠李戴。
    series: str = ""
    label: str  # 原文标签，如 "Figure 1" / "Extended Data Fig. 2"
    is_continued: bool = False  # `Fig. N (continued)` 续页标记
    page: int
    bbox: BBox
    text: str  # 完整图题（可能跨多个文本块拼接）


class FigureAsset(BaseModel):
    """L1 产出的一张图/表。跨页图已合并为一条，images 含多张切图。"""

    figure_id: str  # 规范化标识，如 "Figure 1" / "Extended Data Figure 1" / "Table 3"
    kind: Literal["figure", "table"]
    number: str
    #: 图序列（同 `CaptionAnchor.series`）。空串 = 正文图。
    #: 下游生成文件名时必须带上它，否则正文 Fig.1 与 Extended Data Fig.1 会写到同一个文件。
    series: str = ""
    caption: str
    pages: list[int]  # 该图涉及的所有页码（升序）
    images: list[str]  # 切图路径，相对于输出目录，如 "figures/fig_01.png"
    bboxes: list[BBox] = Field(default_factory=list)  # 与 images 一一对应
    layout_route: LayoutRoute
    bbox_confidence: Confidence
    #: 表格专用：还原出的文本网格，给模型做数字交叉校验（见 feasibility.md §3）
    table_text_grid: Optional[list[list[str]]] = None
    warnings: list[str] = Field(default_factory=list)


class ParsedPaper(BaseModel):
    """L1 总输出。序列化为 `parsed.json`，L2 只读它，不再碰 PDF。"""

    schema_version: str = SCHEMA_VERSION
    pdf_path: str
    title: str = ""
    n_pages: int = 0
    has_text_layer: bool = True  # False 表示扫描版，上层应明确报错
    sections: list[Section] = Field(default_factory=list)
    paragraphs: list[Paragraph] = Field(default_factory=list)
    assets: list[FigureAsset] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ── L2 装配层输出 ───────────────────────────────────────────────────────────


class ContextPack(BaseModel):
    """一张图做抽取所需的全部上下文。L3 只吃它 + 切图，不再回看 PDF。"""

    schema_version: str = SCHEMA_VERSION
    figure_id: str
    kind: Literal["figure", "table"]
    caption: str
    images: list[str]
    pages: list[int]
    #: 正文里引用了本图的段落（含 §id，供 evidence.from_body 填写）
    citing_paragraphs: list[Paragraph] = Field(default_factory=list)
    #: 由图题/引用段的方法学关键词，从 Methods 章节召回的段落
    methods_paragraphs: list[Paragraph] = Field(default_factory=list)
    #: 从图题里切出的子图描述，如 {"A": "CCK-8 assay ...", "B": "..."}
    panel_captions: dict[str, str] = Field(default_factory=dict)
    #: 图题里第一个 `(A)` **之前**的总述文字（实验总体说明、成像参数等），适用于全图
    caption_preamble: str = ""
    #: 最后一个子图描述**之后**的收尾文字，适用于全图。
    #: 这里常藏着最高价值的信息——`N = 3`、`mean ± SD`、`* p<0.05` 的定义。
    #: 必须与末尾 panel 分离，否则 A~C 各 panel 会漏掉图题其实已报告的 n。
    caption_trailer: str = ""
    table_text_grid: Optional[list[list[str]]] = None
    bbox_confidence: Confidence = Confidence.HIGH


# ── L3 抽取层输出 ───────────────────────────────────────────────────────────


class PaperMeta(BaseModel):
    title: str = ""
    year: Optional[int] = None
    venue: str = ""
    doi: str = ""


class Design(BaseModel):
    """实验设计骨架。levels 即分组依据的各个水平。"""

    independent_var: str = NOT_REPORTED
    levels: list[str] = Field(default_factory=list)
    controlled: list[str] = Field(default_factory=list)
    unit: str = NOT_REPORTED  # 实验单位：动物/细胞孔/样本/模型/数据集


class Group(BaseModel):
    """一个实验组。这是跨文献汇总长表的行粒度。"""

    name: str
    role: Literal["treatment", "control", "baseline", "ablation", "reference"]
    condition: str = NOT_REPORTED  # 药物+剂量+途径+时长 / 模型+数据集+超参
    n: str = NOT_REPORTED  # 样本量或重复数
    readout: str = NOT_REPORTED  # 测量指标 + 方法
    result: str = NOT_REPORTED  # 数值 + 方向
    vs_control: str = NOT_REPORTED  # 相对对照的变化


class Stats(BaseModel):
    test: str = NOT_REPORTED
    error_bar: str = NOT_REPORTED  # SD / SEM / CI
    replicates: str = NOT_REPORTED  # 生物学重复 / 技术重复 / seeds
    significance: str = NOT_REPORTED  # `*` 标注含义


class Evidence(BaseModel):
    """逐字段溯源。人工复核时只需重点看 inferred。"""

    from_caption: list[str] = Field(default_factory=list)
    from_body: list[str] = Field(default_factory=list)  # 段落 id，如 "§3.2¶2"
    from_methods: list[str] = Field(default_factory=list)
    from_image_only: list[str] = Field(default_factory=list)
    inferred: list[str] = Field(default_factory=list)  # ← 模型推断，非原文明示


class Panel(BaseModel):
    """子图 = 分析单位。一张图常含多个互相独立的实验。"""

    label: str  # "A" / "B"；整图不分子图时填 "-"
    experiment: str = NOT_REPORTED
    design: Design = Field(default_factory=Design)
    groups: list[Group] = Field(default_factory=list)
    stats: Stats = Field(default_factory=Stats)
    finding: str = NOT_REPORTED
    evidence: Evidence = Field(default_factory=Evidence)


class FigureRecord(BaseModel):
    """L3 总输出，一图一条。序列化为 `figures.json` 的数组元素。"""

    schema_version: str = SCHEMA_VERSION
    paper: PaperMeta = Field(default_factory=PaperMeta)
    domain: Literal["bio", "mlcs"] = "mlcs"  # auto 判定结果
    figure_id: str
    kind: Literal["figure", "table"] = "figure"
    pages: list[int] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    caption: str = ""
    #: schematic（框架示意图）不含实验，panels 应为空
    figure_kind: Literal[
        "experiment", "schematic", "qualitative_example", "analysis"
    ] = "experiment"
    question: str = NOT_REPORTED
    panels: list[Panel] = Field(default_factory=list)
    figure_conclusion: str = NOT_REPORTED
    confidence: Confidence = Confidence.MEDIUM
    open_questions: list[str] = Field(default_factory=list)


class PaperBundle(BaseModel):
    """单篇论文的完整抽取结果。L4 渲染层的唯一输入。"""

    schema_version: str = SCHEMA_VERSION
    paper: PaperMeta = Field(default_factory=PaperMeta)
    domain: Literal["bio", "mlcs"] = "mlcs"
    source_pdf: str = ""
    output_dir: str = ""
    figures: list[FigureRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ── 工具函数 ────────────────────────────────────────────────────────────────


def is_reported(value: Any) -> bool:
    """判断字段是否有实质内容（非 not_reported / 空）。渲染层据此决定是否显示。

    允许 `not_reported（图题收尾只覆盖 c、f、g，未涉及本 panel）` 这种**带理由**的写法：
    抽取时说明「为什么判定为未报告」对复核很有价值（证明确实查过而非漏填），
    但它仍然是未报告。只要以哨兵词开头就一律判为未报告，理由留在数据里备查。
    """
    if value is None:
        return False
    if isinstance(value, str):
        v = value.strip().lower()
        if not v:
            return False
        if v.startswith(NOT_REPORTED):
            return False
        return v not in {"n/a", "na", "未说明", "-", "—", "none", "null"}
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def needs_review(panel: Panel) -> bool:
    """该 panel 是否**自报**含推断字段。注意：仅凭自报不足以判定可信度，
    图级别请用 `review_reasons()`——脑补的记录往往正好谎称 `inferred: []`。"""
    return len(panel.evidence.inferred) > 0


#: 哨兵警告里的 panel 归属标记，形如 `panels[0](A)`。
#: 完整警告格式：`[复核] figure_04.json · Figure 4 panels[0](A): 填了 n='3' 却没…`
_SENTINEL_PANEL_RE = re.compile(r"panels\[\d+\]\(([^)]*)\)")


class ReviewReason(BaseModel):
    """一条「需人工复核」的理由。

    结构化而非拼好的字符串，有两个原因：

    1. **panel 归属不能丢。** 哨兵警告里的 `panels[0](A)` 位于第一个冒号**之前**，
       若只保留冒号之后的正文，多 panel 图就无法定位到底该核哪个 panel。
    2. **渲染层不该靠文案前缀分流。** 用 `kind` 判定来源，改文案不会让下游静默退化。
    """

    #: `inferred` 模型自报推断 · `low_confidence` 自评低置信 · `sentinel` 校验哨兵命中
    kind: Literal["inferred", "low_confidence", "sentinel"]
    #: panel label；`None` 表示这是图级理由，不归属任何单个 panel
    panel: Optional[str] = None
    #: 面向人的说明，渲染层可直接展示
    text: str


def review_reasons(bundle: PaperBundle) -> dict[str, list[ReviewReason]]:
    """按 figure_id 汇总「需人工复核」的全部理由，供 L4 统一打 ⚠。

    三个来源缺一不可：

    1. panel 自报的 `evidence.inferred`
    2. 图自报的 `confidence == low`
    3. **merge 阶段哨兵产生的 `[复核]` 警告** —— 最关键的一路。
       模型若在 `n` / `stats.test` 上脑补，通常同时谎报 `inferred: []`，
       此时只有哨兵的独立判断能抓住它。**自报绝不能作为唯一依据。**

    返回 `{figure_id: [ReviewReason, ...]}`，只含确实需要复核的图。
    """
    out: dict[str, list[ReviewReason]] = {}

    for fig in bundle.figures:
        reasons: list[ReviewReason] = []
        for p in fig.panels:
            if needs_review(p):
                fields = "、".join(p.evidence.inferred)
                reasons.append(
                    ReviewReason(
                        kind="inferred",
                        panel=p.label,
                        text=f"含模型推断字段：{fields}",
                    )
                )
        if fig.confidence == Confidence.LOW:
            reasons.append(
                ReviewReason(kind="low_confidence", text="整图置信度自评为低")
            )
        if reasons:
            out.setdefault(fig.figure_id, []).extend(reasons)

    # 哨兵警告按 figure_id 归位，并抠出 panel 归属
    known = sorted((f.figure_id for f in bundle.figures), key=len, reverse=True)
    for w in bundle.warnings:
        if "[复核]" not in w:
            continue
        fid = next((f for f in known if f in w), None)
        if fid is None:
            continue
        m = _SENTINEL_PANEL_RE.search(w)
        # 正文在第一个冒号之后；panel 标记不含冒号，故这一刀始终切在 tag 与正文之间
        text = w.split(":", 1)[-1].strip() if ":" in w else w.strip()
        out.setdefault(fid, []).append(
            ReviewReason(
                kind="sentinel",
                panel=(m.group(1) or None) if m else None,
                text=text,
            )
        )

    return out
