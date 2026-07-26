"""paper-figure-notes — 文献 figure 实验设计提取流水线。

分层（见 PLAN.md §3）：
    L1 parse   PDF → ParsedPaper（切图 + 章节 + 段落 + 图题）  确定性
    L2 context ParsedPaper → ContextPack[]（每图凑齐上下文）   确定性
    L3 extract 切图 + ContextPack → FigureRecord[]             模型
    L4 render  PaperBundle → 笔记 / 思维导图 / Excel 长表       确定性
"""

from pfn.models import (  # noqa: F401
    NOT_REPORTED,
    SCHEMA_VERSION,
    CaptionAnchor,
    Confidence,
    ContextPack,
    Design,
    Evidence,
    FigureAsset,
    FigureRecord,
    Group,
    LayoutRoute,
    Panel,
    PaperBundle,
    PaperMeta,
    Paragraph,
    ParsedPaper,
    ReviewReason,
    Section,
    Stats,
    is_reported,
    needs_review,
    review_reasons,
)

__version__ = "0.1.0"
