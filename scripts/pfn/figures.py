"""L1 · 图区域检测与切图。

本模块同时承担 L1 的**共享几何原语**（页面索引、分栏、页眉页脚、caption 锚点），
`tables.py` 与 `parse.py` 都从这里导入，避免各自重算 `get_drawings()`（很贵）。

核心算法来自 `references/feasibility.md`，两种排版由一条规则分派：

    页面存在面积 ≥10% 的位图  → JOURNAL_RASTER：直接取 image rect
    否则                      → LATEX_VECTOR ：矢量聚类 + 分栏 + ceiling 规则

切图一律走 `page.get_pixmap(clip=bbox, dpi=dpi)`，**绝不** `get_images()` 抽 XObject
——实测 ACL 样本 11 张图里 7 张是纯矢量，抽取会全部丢失。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz

from pfn.models import BBox, CaptionAnchor, Confidence, FigureAsset, LayoutRoute

# ── 常量 ────────────────────────────────────────────────────────────────────

#: 各类 Unicode 空白。Scientific Reports 的图题用 U+2002 EN SPACE 分隔编号与正文。
_SP = r"[\s\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]"

_FIG_KW = r"figures?|figs?\.?|abbildung|图|圖"
_TAB_KW = r"tables?|tabs?\.?|表"
_PREFIX = r"extended\s+data|supplementary|supp\.?|appendix|附录|补充|補充"

CAPTION_RE = re.compile(
    rf"^{_SP}*(?:(?P<pre>{_PREFIX}){_SP}*)?"
    rf"(?P<kw>{_FIG_KW}|{_TAB_KW})"
    rf"{_SP}*(?P<num>\d{{1,3}}|[IVXLC]{{1,6}}(?![a-z]))"
    rf"(?P<sub>[a-z])?"
    rf"(?={_SP}|[.:：、)\]|]|$)",
    re.IGNORECASE,
)

#: 前缀原文 → 规范序列名。序列决定图号的作用域，见 `CaptionAnchor.series`。
_SERIES_CANON = (
    ("extended", "extended data"),
    ("supp", "supplementary"),
    ("补充", "supplementary"),
    ("補充", "supplementary"),
    ("appendix", "appendix"),
    ("附录", "appendix"),
)

#: 序列名 → 展示前缀 / 文件名前缀。
_SERIES_DISPLAY = {
    "": ("", ""),
    "extended data": ("Extended Data ", "ed"),
    "supplementary": ("Supplementary ", "s"),
    "appendix": ("Appendix ", "ap"),
}

#: Nature 系整页大图常把图题挪到下一页，占位文本形如
#: `Extended Data Fig. 10 | See next page for caption.`
PLACEHOLDER_CAPTION_RE = re.compile(
    r"see\s+(?:the\s+)?(?:next|previous|following)\s+page\s+for\s+(?:the\s+)?caption",
    re.IGNORECASE,
)


def canon_series(prefix: Optional[str]) -> str:
    """把图题前缀归一成序列名。无前缀（正文图）返回空串。"""
    if not prefix:
        return ""
    low = prefix.strip().lower()
    for needle, canon in _SERIES_CANON:
        if needle in low:
            return canon
    return low

CONTINUED_RE = re.compile(r"\(?\s*(continued|cont[’'`]?d\.?|续|續)\s*\)?", re.IGNORECASE)

#: 位图/矢量候选的最小尺寸（pt）。低于此值多为文字装饰、下划线、数学符号碎片。
MIN_IMG_SIDE = 20.0
MIN_DRAW_SIDE = 8.0
#: 单页位图面积占比达到此值即判为期刊排版（feasibility.md §5）。
JOURNAL_AREA_RATIO = 0.10
#: 期刊排版页的位图不应太多——超过这个数说明是 LaTeX 拼贴图。
JOURNAL_MAX_RASTERS = 4
#: 「图页」判定：正文字符少或图占比大（feasibility.md §5.4）。
FIGURE_PAGE_MAX_CHARS = 600
FIGURE_PAGE_AREA = 0.20
#: 候选图形聚类时允许的垂直断裂，超过则认为不属于同一张图。
CLUSTER_GAP = 34.0
#: 聚类结果占「全部候选并集」的最低面积比。低于此值判为被断裂截断，尝试扩展。
COVERAGE_MIN = 0.60
#: 被丢弃区域里允许出现的整行正文数。超过就说明那里真的是正文，不能并进图里。
MAX_BODY_LINES_IN_FIGURE = 2
#: 判定「整行正文」的宽度下限（相对栏宽）。图内的 panel 标号、坐标轴刻度都远短于此。
BODY_LINE_MIN_W = 0.40
#: 图形 bbox 的最小边长与最小页面占比，低于此值多半切歪了
MIN_FIGURE_SIDE = 24.0
MIN_FIGURE_AREA_RATIO = 0.015
#: bbox 四周留白
PAD = 4.0
#: 降级带的合理高度区间（相对页高）
BAND_MIN_H = 40.0
BAND_MAX_RATIO = 0.80


# ── 小工具 ──────────────────────────────────────────────────────────────────


def norm_ws(text: str) -> str:
    """把各类 Unicode 空白归一为普通空格并压缩。用于正则匹配与展示。"""
    return re.sub(r"[\s\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+", " ", text).strip()


def _area(r: fitz.Rect) -> float:
    return max(0.0, r.x1 - r.x0) * max(0.0, r.y1 - r.y0)


def _to_bbox(r: fitz.Rect) -> BBox:
    return (round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2))


def _overlap_1d(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def figure_slug(kind: str, number: str, series: str = "") -> str:
    """`fig_03` / `tab_12` / `edfig_01`（Extended Data）。

    序列前缀不可省——正文 Fig.1 与 Extended Data Fig.1 同号，共用 slug 会互相覆盖切图。
    """
    head = ("fig" if kind == "figure" else "tab")
    head = _SERIES_DISPLAY.get(series, ("", series.replace(" ", "")))[1] + head
    try:
        return f"{head}_{int(number):02d}"
    except ValueError:
        return f"{head}_{re.sub(r'[^0-9A-Za-z]+', '', number).lower() or 'x'}"


# ── caption 锚点 ────────────────────────────────────────────────────────────


def find_caption_anchors(blocks: list, pno: int) -> list[CaptionAnchor]:
    """扫描页面文本块，命中图题/表题正则的作为锚点。

    只在**块首**匹配，避免正文里的 "…as shown in Figure 3…" 误命中。
    实测 ACL 11/11 图 + 19/19 表、SciRep 5/5 图（含 continued）全部命中且零误报。
    """
    anchors: list[CaptionAnchor] = []
    for b in blocks:
        raw = b[4]
        flat = norm_ws(raw)
        if not flat:
            continue
        m = CAPTION_RE.match(flat)
        if not m:
            continue
        kw = m.group("kw").lower()
        kind = "table" if re.match(rf"^(?:{_TAB_KW})$", kw, re.IGNORECASE) else "figure"
        head = flat[: m.end()]
        tail_head = flat[m.end() : m.end() + 40]
        text, rect = _extend_caption(fitz.Rect(b[:4]), flat, b[4], blocks)
        anchors.append(
            CaptionAnchor(
                kind=kind,
                number=m.group("num").upper(),
                series=canon_series(m.group("pre")),
                label=head.strip(),
                is_continued=bool(CONTINUED_RE.search(tail_head)),
                page=pno + 1,
                bbox=_to_bbox(rect),
                text=text[:2000],
            )
        )
    anchors.sort(key=lambda a: (a.bbox[1], a.bbox[0]))
    return _dedupe_anchors(anchors)


def _extend_caption(
    rect: fitz.Rect, flat: str, raw: str, blocks: list
) -> tuple[str, fitz.Rect]:
    """图题被拆成多个 block 时接上后续块。

    行内数学会把一条图题切开——实测 ACL 的 Figure 3 因为下标 `S_D′` 断成两块，
    只取首块得到的图题是半句话，下游模型直接失去一半信息。
    只接**明显是续写**的块：以小写起头，或与当前块纵向交叠（数学断块的典型特征）。
    """
    line_h = max(8.0, rect.height / max(1, raw.count("\n")))
    text, box = flat, fitz.Rect(rect)
    tail = sorted(
        (b for b in blocks if fitz.Rect(b[:4]).y0 >= rect.y0 and fitz.Rect(b[:4]) != rect),
        key=lambda b: b[1],
    )
    for b in tail:
        r = fitz.Rect(b[:4])
        if _overlap_1d(r.x0, r.x1, box.x0, box.x1) < 0.6 * min(r.width, box.width):
            continue
        if r.y0 - box.y1 > 0.5 * line_h:
            # 被公式切开的续块与本块纵向交叠（间距为负），而图题下面那段正文
            # 隔着完整一行的行距。阈值必须卡在这两者之间，否则会把正文吸进图题。
            break
        piece = norm_ws(b[4])
        if not piece or CAPTION_RE.match(piece):
            break
        if not (piece[0].islower() or r.y0 < box.y1):
            break
        text = f"{text} {piece}"
        box |= r
    return text, box


def _dedupe_anchors(anchors: list[CaptionAnchor]) -> list[CaptionAnchor]:
    """同页同编号重复命中时保留正文最长的一条（另一条多半是页内引用残块）。"""
    best: dict[tuple[str, str, str, bool], CaptionAnchor] = {}
    for a in anchors:
        key = (a.kind, a.series, a.number, a.is_continued)
        cur = best.get(key)
        if cur is None or len(a.text) > len(cur.text):
            best[key] = a
    return sorted(best.values(), key=lambda a: (a.bbox[1], a.bbox[0]))


# ── 页面索引 ────────────────────────────────────────────────────────────────


@dataclass
class PageInfo:
    """单页预计算结果。`get_drawings()` 很贵，全流程只算一次。"""

    pno: int  # 0-based
    rect: fitz.Rect
    n_chars: int
    columns: list[tuple[float, float]]
    content_top: float
    content_bottom: float
    img_rects: list[fitz.Rect]
    draw_rects: list[fitz.Rect]
    hrules: list[fitz.Rect]  # 细横线：三线表的表线
    text_rects: list[fitz.Rect]
    words: list[tuple]  # get_text("words") 原始结果，表格通道 B 复用
    anchors: list[CaptionAnchor]
    route: LayoutRoute
    #: TextPage 只对 Page 持弱引用，Page 一被回收就用不了了——必须一起留住。
    page: Optional[fitz.Page] = None
    textpage: object = None  # 复用给 parse.py 取 dict，别再重建（每次约 0.2s）

    @property
    def page_no(self) -> int:
        """1-based 页码，对外一律用它。"""
        return self.pno + 1

    @property
    def content_x0(self) -> float:
        return min(c[0] for c in self.columns)

    @property
    def content_x1(self) -> float:
        return max(c[1] for c in self.columns)

    @property
    def raster_area_ratio(self) -> float:
        total = _area(self.rect) or 1.0
        return sum(_area(r) for r in self.img_rects) / total

    def is_figure_page(self) -> bool:
        """整页图页（feasibility.md §5.4）。跨页配对时只信这种页面。"""
        ratio = self.raster_area_ratio
        return (self.n_chars < FIGURE_PAGE_MAX_CHARS and ratio > 0.10) or ratio > FIGURE_PAGE_AREA


@dataclass
class DocIndex:
    """全文预计算索引。L1 三个模块共用。"""

    doc: fitz.Document
    order: list[int]  # 参与处理的 0-based 页序号（升序）
    pages: dict[int, PageInfo] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def get(self, pno: int) -> Optional[PageInfo]:
        return self.pages.get(pno)

    def neighbor(self, pno: int, step: int) -> Optional[PageInfo]:
        """参与处理的相邻页（跳过被 --pages 过滤掉的页）。"""
        try:
            i = self.order.index(pno)
        except ValueError:
            return None
        j = i + step
        if 0 <= j < len(self.order):
            return self.pages[self.order[j]]
        return None

    @property
    def total_chars(self) -> int:
        return sum(p.n_chars for p in self.pages.values())


def build_doc_index(doc: fitz.Document, page_ids: list[int]) -> DocIndex:
    """一次性预计算全部页面。

    性能上有两处非做不可的优化（ACL 24 页样本从 80s 降到 ~12s）：
    每页只建**一个 TextPage** 供 blocks/words/dict 复用（每次重建约 0.2s），
    以及用 `get_cdrawings()` 取矢量图元——它返回纯 tuple，省掉给三万多个图元
    逐个构造 Point/Rect 对象的开销。
    """
    idx = DocIndex(doc=doc, order=sorted(page_ids))
    raw: dict[int, tuple[fitz.Page, object, list, list]] = {}
    for pno in idx.order:
        page = doc[pno]
        tp = page.get_textpage()
        raw[pno] = (
            page,
            tp,
            [b for b in page.get_text("blocks", textpage=tp) if b[6] == 0],
            page.get_text("words", textpage=tp),
        )
    bands = _detect_running_bands(raw, idx.order)

    lines_by_page: dict[int, list[fitz.Rect]] = {}
    for pno in idx.order:
        top, bottom = bands.get(pno, (raw[pno][0].rect.y0, raw[pno][0].rect.y1))
        lines_by_page[pno] = [
            r
            for r in line_rects_from_words(raw[pno][3])
            if r.y1 > top and r.y0 < bottom
        ]
    layout = _doc_columns(raw[idx.order[0]][0].rect, lines_by_page)

    for pno in idx.order:
        page, tp, blocks, words = raw[pno]
        idx.pages[pno] = _build_page_info(
            page, pno, bands.get(pno), tp, blocks, words, lines_by_page[pno], layout
        )
    return idx


def _build_page_info(
    page: fitz.Page,
    pno: int,
    band: Optional[tuple[float, float]],
    textpage,
    blocks: list,
    words: list,
    line_rects: list[fitz.Rect],
    layout: list[tuple[float, float]],
) -> PageInfo:
    rect = fitz.Rect(page.rect)
    top, bottom = band if band else (rect.y0, rect.y1)

    text_rects = [fitz.Rect(b[:4]) for b in blocks]

    img_rects: list[fitz.Rect] = []
    seen: set[tuple[int, int, int, int]] = set()
    for meta in page.get_images(full=True):
        try:
            rects = page.get_image_rects(meta[0])
        except Exception:  # 损坏的 xref，跳过而非中断
            continue
        for r in rects:
            r = fitz.Rect(r) & rect
            if r.width < MIN_IMG_SIDE or r.height < MIN_IMG_SIDE:
                continue
            key = (int(r.x0), int(r.y0), int(r.x1), int(r.y1))
            if key in seen:
                continue
            seen.add(key)
            img_rects.append(r)

    draw_rects: list[fitz.Rect] = []
    hrules: list[fitz.Rect] = []
    page_area = _area(rect) or 1.0
    for d in page.get_cdrawings():  # rect 是 tuple，先用裸数值筛，别急着建对象
        x0, y0, x1, y1 = d["rect"]
        w, h = x1 - x0, y1 - y0
        if h <= 3.0 and w > 60.0:
            hrules.append(fitz.Rect(x0, y0, x1, y1) & rect)
        if w < MIN_DRAW_SIDE or h < MIN_DRAW_SIDE or w * h > 0.92 * page_area:
            continue
        r = fitz.Rect(x0, y0, x1, y1) & rect
        if not r.is_empty:
            draw_rects.append(r)

    info = PageInfo(
        pno=pno,
        rect=rect,
        n_chars=sum(len(b[4].strip()) for b in blocks),
        columns=_page_columns(rect, line_rects, layout),
        content_top=top,
        content_bottom=bottom,
        img_rects=img_rects,
        draw_rects=draw_rects,
        hrules=hrules,
        text_rects=text_rects,
        words=words,
        anchors=find_caption_anchors(blocks, pno),
        route=LayoutRoute.LATEX_VECTOR,
        page=page,
        textpage=textpage,
    )
    info.route = _page_route(info)
    return info


def _page_route(info: PageInfo) -> LayoutRoute:
    """排版分派：单张大位图 → 期刊路线；否则 LaTeX 路线。"""
    page_area = _area(info.rect) or 1.0
    big = [r for r in info.img_rects if _area(r) / page_area >= JOURNAL_AREA_RATIO]
    if big and len(info.img_rects) <= JOURNAL_MAX_RASTERS:
        return LayoutRoute.JOURNAL_RASTER
    return LayoutRoute.LATEX_VECTOR


def line_rects_from_words(words: list[tuple]) -> list[fitz.Rect]:
    """把 `get_text("words")` 按 (block_no, line_no) 还原成文本行矩形。

    比 `get_text("blocks")` 采样密得多——分栏判定必须用行级，块级在图表页只有
    三五个样本，会把双栏误判成单栏，进而毁掉段落切分。
    """
    groups: dict[tuple[int, int], fitz.Rect] = {}
    for w in words:
        if not w[4].strip():
            continue
        key = (w[5], w[6])
        r = fitz.Rect(w[:4])
        groups[key] = (groups[key] | r) if key in groups else r
    return list(groups.values())


def _doc_columns(
    rect: fitz.Rect, lines_by_page: dict[int, list[fitz.Rect]]
) -> list[tuple[float, float]]:
    """**全文级**栏布局。栏边界必须放到全文算，不能逐页算。

    附录里整页都是表格的页面根本没有正文左边距可学，逐页统计会得出
    (255,295) 这种荒唐的栏宽；而同一篇论文的正文页边距是恒定的，
    汇总几千行样本后众数极其干净。
    """
    pooled = [r for rs in lines_by_page.values() for r in rs]
    if not pooled:
        return [(rect.x0, rect.x1)]
    mid = (rect.x0 + rect.x1) / 2
    narrow = [r for r in pooled if r.width < 0.55 * rect.width]
    left = [r for r in narrow if (r.x0 + r.x1) / 2 < mid]
    right = [r for r in narrow if (r.x0 + r.x1) / 2 >= mid]
    if len(left) >= 30 and len(right) >= 30 and min(len(left), len(right)) >= 0.2 * len(narrow):
        return [_column_edges(left), _column_edges(right)]
    return [_column_edges(narrow or pooled)]


def _page_columns(
    rect: fitz.Rect, line_rects: list[fitz.Rect], layout: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """本页用几栏——边界沿用全文布局，只判断本页是否真的分了栏。"""
    if len(layout) < 2:
        return list(layout)
    (l0, l1), (r0, r1) = layout
    gutter = (l1 + r0) / 2
    narrow = [r for r in line_rects if r.width < 0.55 * rect.width]
    left = sum(1 for r in narrow if (r.x0 + r.x1) / 2 < gutter)
    right = len(narrow) - left
    if left >= 4 and right >= 4:
        return list(layout)
    return [(l0, r1)]  # 通栏页（整页大图、整页宽表）


def _column_edges(rects: list[fitz.Rect]) -> tuple[float, float]:
    """栏边界取「左边界众数 + 贴左行的 x1 高分位」，而不是 min/max。

    图内的坐标轴刻度、图例散落在整栏各处，min/max 会被它们拽歪——实测 ACL 首页
    因此把右栏算成 (148,526)，直接毁掉段落切分。正文左边距极其稳定，用它当锚点最可靠。
    """
    left = _mode([r.x0 for r in rects], bin_size=2.0)
    aligned = [r for r in rects if abs(r.x0 - left) <= 4.0]
    xs = sorted(r.x1 for r in (aligned if len(aligned) >= 4 else rects))
    right = xs[int(0.90 * (len(xs) - 1))]
    return (left, max(right, left + 40.0))


def _mode(values, bin_size: float) -> float:
    """按 bin 聚类求众数，返回该 bin 内的最小值。"""
    buckets: dict[int, list[float]] = {}
    for v in values:
        buckets.setdefault(int(v / bin_size), []).append(v)
    if not buckets:
        return 0.0
    best = max(buckets.values(), key=len)
    return min(best)


def _detect_running_bands(
    raw: dict[int, tuple], page_ids: list[int]
) -> dict[int, tuple[float, float]]:
    """识别页眉/页脚带，返回每页的正文上下边界。

    判据：位于页面顶部 8.5% / 底部 10%，且**数字归一后的模板在多页重复**。
    实测能同时吃掉 Nature 系的 `www.nature.com/scientificreports/` 页眉、
    `Scientific Reports | (2026) 16:18069 N | https://doi.org/…` 页脚，
    以及 ACL 的纯页码（模板归一成 `#`）。
    """
    if not page_ids:
        return {}
    top_hits: dict[str, set[int]] = {}
    bot_hits: dict[str, set[int]] = {}
    cache: dict[int, list[tuple[fitz.Rect, str, bool]]] = {}

    for pno in page_ids:
        page = raw[pno][0]
        ph = page.rect.height
        top_lim = page.rect.y0 + 0.085 * ph
        bot_lim = page.rect.y1 - 0.100 * ph
        rows: list[tuple[fitz.Rect, str, bool]] = []
        for b in raw[pno][2]:
            txt = norm_ws(b[4])
            if not txt or len(txt) > 160:
                continue
            r = fitz.Rect(b[:4])
            tmpl = re.sub(r"\d+", "#", txt)[:90]
            if r.y1 <= top_lim:
                rows.append((r, tmpl, True))
                top_hits.setdefault(tmpl, set()).add(pno)
            elif r.y0 >= bot_lim:
                rows.append((r, tmpl, False))
                bot_hits.setdefault(tmpl, set()).add(pno)
        cache[pno] = rows

    need = max(3, int(0.25 * len(page_ids)))
    running_top = {t for t, ps in top_hits.items() if len(ps) >= need}
    running_bot = {t for t, ps in bot_hits.items() if len(ps) >= need}

    bands: dict[int, tuple[float, float]] = {}
    for pno in page_ids:
        page = raw[pno][0]
        top, bottom = page.rect.y0, page.rect.y1
        for r, tmpl, is_top in cache[pno]:
            if is_top and tmpl in running_top:
                top = max(top, r.y1 + 2.0)
            elif not is_top and tmpl in running_bot:
                bottom = min(bottom, r.y0 - 2.0)
        if bottom - top < 0.4 * page.rect.height:  # 判失控，放弃排除
            top, bottom = page.rect.y0, page.rect.y1
        bands[pno] = (top, bottom)
    return bands


# ── 分栏归属与 ceiling ──────────────────────────────────────────────────────


def caption_column(info: PageInfo, cap: fitz.Rect) -> tuple[float, float]:
    """caption 所属栏。横跨 ≥66% 正文宽的（如 ACL 的通栏图）返回整个正文宽度。"""
    content_w = info.content_x1 - info.content_x0
    if content_w > 0 and cap.width >= 0.66 * content_w:
        return (info.content_x0, info.content_x1)
    cx = (cap.x0 + cap.x1) / 2
    for c0, c1 in info.columns:
        if c0 - 6 <= cx <= c1 + 6:
            return (c0, c1)
    return (info.content_x0, info.content_x1)


def _shares_column(a: fitz.Rect, col: tuple[float, float]) -> bool:
    """两个横向区间重叠超过窄者的 30% 即视为同栏。"""
    ov = _overlap_1d(a.x0, a.x1, col[0], col[1])
    narrow = min(a.width, col[1] - col[0])
    return narrow > 0 and ov > 0.30 * narrow


def ceiling_for(info: PageInfo, cap: fitz.Rect, col: tuple[float, float]) -> float:
    """天花板 = 同栏内位于本 caption 上方最近的一个 caption 的底边。

    这是「一页多图不互相吞并」的关键（feasibility.md §2）。实测 ACL page 7 的
    Figure 6 由此拿到 ceiling=316，精确避开上方的 Table 4。
    """
    ceiling = info.content_top
    for other in info.anchors:
        orect = fitz.Rect(other.bbox)
        if orect == cap:
            continue
        if not _shares_column(orect, col):
            continue
        if orect.y1 <= cap.y0 + 1 and orect.y1 > ceiling:
            ceiling = orect.y1
    return ceiling


def floor_for(info: PageInfo, cap: fitz.Rect, col: tuple[float, float]) -> float:
    """地板 = 同栏内位于本 caption 下方最近的一个 caption 的顶边。"""
    floor = info.content_bottom
    for other in info.anchors:
        orect = fitz.Rect(other.bbox)
        if orect == cap:
            continue
        if not _shares_column(orect, col):
            continue
        if orect.y0 >= cap.y1 - 1 and orect.y0 < floor:
            floor = orect.y0
    return floor


# ── LaTeX 路线：矢量聚类 ────────────────────────────────────────────────────


def _effective_column(
    info: PageInfo, cap: fitz.Rect, col: tuple[float, float], ceiling: float
) -> tuple[float, float]:
    """图形明显跨栏时改用整幅内容宽度。

    Nature 系常见排法是「整幅大图 + 单栏图题」：图题只占左栏，图却横跨两栏
    （实测 Nature Genetics 的 Fig.1 图题 x=40–292，图 x=55–561）。
    按图题所在栏去裁就会切掉右半张图，只剩贴着图题的那一个 panel。

    但真·双栏并排两图（ACL 的 Fig.7 左 / Fig.8 右）绝不能合并，
    故要求：另一栏在本纵向区间内**没有别的图题**，才认定是跨栏大图。
    """
    if len(info.columns) < 2:
        return col
    band = [r for r in (info.img_rects + info.draw_rects) if ceiling < r.y1 <= cap.y0 + 2]
    if not band:
        return col
    x0, x1 = min(r.x0 for r in band), max(r.x1 for r in band)
    if x0 >= col[0] - 24 and x1 <= col[1] + 24:
        return col  # 没越出本栏

    for a in info.anchors:
        if a.kind != "figure" or tuple(a.bbox) == tuple(cap):
            continue
        r = fitz.Rect(a.bbox)
        cx = (r.x0 + r.x1) / 2
        if not (col[0] <= cx < col[1]) and r.y0 > ceiling:
            return col  # 另一栏另有图题 → 并排两图，各归各栏
    return (info.content_x0, info.content_x1)


def _body_lines_in(
    info: PageInfo,
    region: fitz.Rect,
    col: tuple[float, float],
    graphics: Optional[list[fitz.Rect]] = None,
) -> int:
    """区域内的**正文**行数——只数不压在任何图形上的宽文本。

    单看宽度不够：图里的流程图标签、表格行、跨 panel 的小标题本身就很宽，
    会被误判成正文，于是「中间夹着正文所以不能扩展」的判定失灵
    （实测 Nature Genetics 的 Fig.1 因此只切出队列表一格）。

    真正的区分点是**底下有没有图形**：坐标轴刻度、图例、表格行都紧贴着
    绘制元素（底纹、轴线、边框），而正文段落下方是空的。
    """
    if region.is_empty or region.height <= 0:
        return 0
    gfx = graphics if graphics is not None else (info.img_rects + info.draw_rects)
    min_w = BODY_LINE_MIN_W * max(1.0, col[1] - col[0])
    n = 0
    for t in info.text_rects:
        if t.width < min_w:
            continue
        if not (
            _overlap_1d(t.y0, t.y1, region.y0, region.y1) > 0.6 * max(1.0, t.height)
            and _overlap_1d(t.x0, t.x1, region.x0, region.x1) > 0.5 * max(1.0, t.width)
        ):
            continue
        on_graphic = any(
            _overlap_1d(t.x0, t.x1, g.x0, g.x1) > 0.5 * max(1.0, t.width)
            and _overlap_1d(t.y0, t.y1, g.y0, g.y1) > 0.5 * max(1.0, t.height)
            for g in gfx
        )
        if not on_graphic:
            n += 1
    return n


def latex_graphics_bbox(
    info: PageInfo, cap: fitz.Rect, col: tuple[float, float], ceiling: float
) -> tuple[Optional[fitz.Rect], float]:
    """caption 上方、ceiling 下方、与本栏横向重叠的图形并集。

    返回 ``(bbox, coverage)``。`coverage` 是所取区域占全部候选并集的面积比，
    调用方据此决定置信度——**覆盖率低就不许报 high**，这是漏切的唯一外部信号。
    """
    cands = [
        r
        for r in (info.img_rects + info.draw_rects)
        if r.y1 <= cap.y0 + 2
        and r.y1 > ceiling + 2
        and _overlap_1d(r.x0, r.x1, col[0], col[1]) > 0.25 * min(r.width, col[1] - col[0])
    ]
    if not cands:
        return None, 0.0

    full = fitz.Rect(cands[0])
    for r in cands[1:]:
        full |= r

    # 从最贴近 caption 的簇往上逐个合并，**只在簇间缝隙里出现正文时才停**。
    #
    # 早先的做法是遇到 >CLUSTER_GAP 的断裂就停（为了不把 LaTeX 正文里的高大数学
    # 公式吸进图里）。但 Nature 多联图的行间距经常超过这个阈值，于是只切出贴着
    # 图题的那一行 panel——实测 Fig.1 八个子图只剩队列表一个，还报 high 置信度。
    #
    # 关键在于「缝隙里有什么」而不是「缝隙有多宽」：图内空隙是空的，
    # 而两张图之间隔着的正文会把缝隙填满。只看缝隙也避免了把子图自身的宽文本
    # （相关系数标注、表格行、panel 列标题）误判成正文。
    clusters = _vertical_clusters(cands)
    box = fitz.Rect(clusters[-1])
    for c in reversed(clusters[:-1]):
        gap = fitz.Rect(full.x0, c.y1, full.x1, box.y0)
        if _body_lines_in(info, gap, col, cands) > MAX_BODY_LINES_IN_FIGURE:
            break
        box |= c

    coverage = _area(box) / (_area(full) or 1.0)
    # 越过天花板的部分裁掉（大图外框常比 ceiling 高几 pt）
    box.y0 = max(box.y0, ceiling)
    box.y1 = min(box.y1, cap.y0)
    box.x0 = max(box.x0, col[0] - 12)
    box.x1 = min(box.x1, col[1] + 12)
    # 尺寸下限不能只防「空」，还得防「碎」：图题正好在页顶时，它上方那条 20pt 的
    # 空白也会凑出一个候选，认下来就等于自信满满地切出一条废条。
    if box.width < MIN_FIGURE_SIDE or box.height < MIN_FIGURE_SIDE:
        return None, 0.0
    return box, coverage


def _cluster_near_caption(rects: list[fitz.Rect], cap_y0: float) -> list[fitz.Rect]:
    """从最贴近 caption 的候选往上生长，遇到 >CLUSTER_GAP 的垂直断裂就停。

    防止正文里的高大数学公式（分式线、大括号）被误吸进图里。
    """
    ordered = sorted(rects, key=lambda r: -r.y1)
    keep = [ordered[0]]
    top = ordered[0].y0
    for r in ordered[1:]:
        if r.y1 >= top - CLUSTER_GAP:
            keep.append(r)
            top = min(top, r.y0)
    return keep


def absorb_nearby_text(
    info: PageInfo, box: fitz.Rect, col: tuple[float, float], ceiling: float, cap_y0: float
) -> fitz.Rect:
    """把落在图形范围内/紧贴其边缘的文本块并进 bbox（坐标轴刻度、图例、panel 标号）。"""
    grown = fitz.Rect(box)
    probe = fitz.Rect(box.x0 - 14, box.y0 - 14, box.x1 + 14, box.y1 + 14)
    for t in info.text_rects:
        if t.y0 < ceiling - 1 or t.y1 > cap_y0 + 1:
            continue
        if _overlap_1d(t.x0, t.x1, col[0], col[1]) <= 0:
            continue
        inter = fitz.Rect(t) & probe
        if _area(t) > 0 and _area(inter) / _area(t) > 0.60:
            grown |= t
    grown.y0 = max(grown.y0, ceiling)
    grown.y1 = min(grown.y1, cap_y0)
    grown.x0 = max(grown.x0, col[0] - 12)
    grown.x1 = min(grown.x1, col[1] + 12)
    return grown


# ── 切图 ────────────────────────────────────────────────────────────────────


def column_limits(info: PageInfo, col: tuple[float, float]) -> tuple[float, float]:
    """本栏允许扩展到的左右硬边界——绝不越过相邻栏。

    图形外框常比栏宽几 pt，留白也要往外扩，但一旦扩过中缝就会把隔壁栏的正文
    切进来（实测 Figure 6 因此吃到左栏的行尾字符）。
    """
    lo, hi = col[0] - 12.0, col[1] + 12.0
    for c0, c1 in info.columns:
        if c1 <= col[0]:
            lo = max(lo, c1 + 2.0)
        if c0 >= col[1]:
            hi = min(hi, c0 - 2.0)
    return lo, hi


def finalize_bbox(
    info: PageInfo, box: fitz.Rect, limit: Optional[fitz.Rect] = None, pad: float = PAD
) -> fitz.Rect:
    """留白 → 收进硬边界 → 收进正文带（排除页眉页脚）→ 与页面取交。

    顺序很重要：留白**必须先加再裁**。先裁后加会让 4pt 留白重新越过天花板和
    图题顶边，把上一条 caption 的末行和本图图题的首行一起切进来。
    """
    out = fitz.Rect(box.x0 - pad, box.y0 - pad, box.x1 + pad, box.y1 + pad)
    if limit is not None:
        out &= limit
    out.y0 = max(out.y0, info.content_top)
    out.y1 = min(out.y1, info.content_bottom)
    out &= info.rect
    return out


def render_clip(page: fitz.Page, box: fitz.Rect, dpi: int, dest: Path) -> bool:
    """渲染裁剪区域为 PNG。Windows 中文路径下必须用 write_bytes，不能用 cv2。"""
    if box.is_empty or box.width < 4 or box.height < 4:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    pix = page.get_pixmap(clip=box, dpi=dpi)
    dest.write_bytes(pix.tobytes("png"))
    return True


# ── 图：配对 + 合并 + 产出 ──────────────────────────────────────────────────


@dataclass
class _Placement:
    """一个 caption 最终定位到的切图区域。"""

    anchor: CaptionAnchor
    pno: int
    box: fitz.Rect
    route: LayoutRoute
    confidence: Confidence
    warnings: list[str] = field(default_factory=list)
    #: 只贡献图题文字、不产出切图。Nature 系把整页大图的图题单独排一页，
    #: 那一页没有图形却是**真正的图题所在**（实测 116 字的图页 vs 1385 字的图题页），
    #: 既不能丢掉它的文字，也不能让它去邻页抢一张图。
    text_only: bool = False


def _anchor_identity(a: CaptionAnchor) -> tuple[str, str, str]:
    """图的身份。图号只在序列内唯一，故必须带上 series。"""
    return (a.kind, a.series, a.number)


def _has_graphics(info: PageInfo) -> bool:
    """页面上有没有够大的图形。够不上就是纯文字页（Nature 的图题页）。"""
    thr = MIN_FIGURE_AREA_RATIO * _area(info.rect)
    return any(_area(r) >= thr for r in info.img_rects) or (
        sum(_area(r) for r in info.draw_rects) >= thr
    )


def _caption_only_indices(idx: DocIndex, anchors: list[CaptionAnchor]) -> set[int]:
    """找出「同一张图的另一个锚点已经落在有图的页上」的纯文字锚点。

    Nature 的 Extended Data 每张图占两页：图页只写 `Extended Data Fig. 3`，
    下一页才是完整图题。两个锚点同号，若各自都去定位就会切出两张图，
    而第二张必然是从邻页抢来的——那页属于下一张图（实测 Fig.3 与 Fig.4 共享了 p25）。
    """
    with_gfx: set[tuple[str, str, str]] = set()
    for a in anchors:
        if _has_graphics(idx.pages[a.page - 1]):
            with_gfx.add(_anchor_identity(a))

    out: set[int] = set()
    for i, a in enumerate(anchors):
        if _anchor_identity(a) in with_gfx and not _has_graphics(idx.pages[a.page - 1]):
            out.add(i)
    return out


def build_figure_assets(idx: DocIndex, out_dir: Path, dpi: int) -> list[FigureAsset]:
    """图题定位 → 三级回退配对 → `(continued)` 合并 → 切图。"""
    anchors: list[CaptionAnchor] = []
    for pno in idx.order:
        anchors.extend(a for a in idx.pages[pno].anchors if a.kind == "figure")
    anchors.sort(key=lambda a: (a.page, a.bbox[1], a.bbox[0]))
    if not anchors:
        return []

    caption_only = _caption_only_indices(idx, anchors)
    #: 已占用的资源。两种键：`(pno, x0, y0, x1, y1)` 是具体图形，
    #: `("page", pno)` 是整页——跨页配对时一页只供一张图。
    claimed: set = set()
    placements: list[_Placement] = []
    for i, a in enumerate(anchors):
        if i in caption_only:
            placements.append(
                _Placement(
                    a, a.page - 1, fitz.Rect(), LayoutRoute.JOURNAL_RASTER,
                    Confidence.HIGH, text_only=True,
                )
            )
        else:
            placements.append(_place_figure(idx, a, claimed))
    _check_number_order([p for p in placements if not p.text_only])
    return _emit_assets(idx, placements, out_dir, dpi, kind="figure")


def _place_figure(
    idx: DocIndex, anchor: CaptionAnchor, claimed: set
) -> _Placement:
    """三级回退：同页就近 → 上一页尾部 → 下一页顶部；再不行走降级兜底。"""
    pno = anchor.page - 1
    info = idx.pages[pno]
    cap = fitz.Rect(anchor.bbox)
    col = caption_column(info, cap)
    # 跨栏大图要先把栏放宽再算天花板，否则天花板会按窄栏算错
    col = _effective_column(info, cap, col, ceiling_for(info, cap, col))
    ceiling = ceiling_for(info, cap, col)
    lo_x, hi_x = column_limits(info, col)
    limit = fitz.Rect(lo_x, ceiling, hi_x, cap.y0)

    # ① 期刊路线：同页 caption 上方的大位图
    hit = _claim_raster(info, claimed, above_y=cap.y0, col=col)
    if hit is not None:
        return _Placement(
            anchor, pno, finalize_bbox(info, hit, fitz.Rect(info.rect.x0, ceiling, info.rect.x1, cap.y0)),
            LayoutRoute.JOURNAL_RASTER, Confidence.HIGH,
        )

    # ② LaTeX 路线：同页矢量/位图聚类
    box, coverage = latex_graphics_bbox(info, cap, col, ceiling)
    if box is not None:
        box = finalize_bbox(info, absorb_nearby_text(info, box, col, ceiling, cap.y0), limit)
        small = _area(box) < MIN_FIGURE_AREA_RATIO * _area(info.rect)
        # 覆盖率低 = 本栏还有图形没被切进来。可能是被正文正当隔开，也可能是漏切了，
        # 无法就地分辨——但**绝不能报 high**，否则下游会把残图当完整图去读。
        partial = coverage < COVERAGE_MIN
        warns: list[str] = []
        if small:
            warns.append(f"检出图形偏小（{box.width:.0f}×{box.height:.0f}pt），请人工确认")
        if partial:
            warns.append(
                f"本栏仅覆盖 {coverage:.0%} 的候选图形，可能有子图未切进来，请人工确认完整性"
            )
        return _Placement(
            anchor, pno, box, LayoutRoute.LATEX_VECTOR,
            Confidence.MEDIUM if (small or partial) else Confidence.HIGH,
            warns,
        )

    # ③ 跨页配对
    #
    # 图题页**自己没有图形**时，图必然在别页——此时若还坚持「只信整页图页」，
    # 就会落到降级分支去纯文字页上切一条带，切出来的是正文而不是图
    # （实测 Nature Genetics 的 Fig.5 因此切出了一整段正文，还标着 medium）。
    # 所以：图题页无图形时放宽判定（邻页只要有实质图形即可）并把搜索扩到 ±3 页。
    #
    # 方向由图题位置决定：图题贴着页顶 = 它是上一页那张图的续尾（Scientific Reports
    # 的排法）；否则图在图题之后（Nature 把图题排在正文页、图另起一页）。
    bare = not _has_graphics(info)
    near_top = cap.y0 < info.content_top + 0.15 * info.rect.height
    dirs = ((-1, "上一页"), (1, "下一页")) if near_top else ((1, "下一页"), (-1, "上一页"))
    max_step = 3 if bare else 1
    me = _anchor_identity(anchor)

    for step, where in dirs:
        for k in range(1, max_step + 1):
            nb = idx.neighbor(pno, step * k)
            if nb is None:
                continue
            if not (nb.is_figure_page() or (bare and _has_graphics(nb))):
                continue
            # 那一页若有自己的图题，它就属于那张图，不能来抢。
            # 实测 Nature 的 Extended Data Fig.3 会一路抢到 Fig.4 的页面上去。
            if any(a.kind == "figure" and _anchor_identity(a) != me for a in nb.anchors):
                continue
            # 一页只供一张图：同页两条图题（Fig.4 与 Fig.5 同在 p8）必须落到不同页
            if ("page", nb.pno) in claimed:
                continue

            note = f"图题在 p{anchor.page}，图取自{where} p{nb.page_no}"
            hit = _claim_raster(nb, claimed, above_y=None, col=None)
            if hit is not None:
                claimed.add(("page", nb.pno))
                return _Placement(
                    anchor, nb.pno, finalize_bbox(nb, hit),
                    LayoutRoute.JOURNAL_RASTER, Confidence.MEDIUM, [note],
                )
            # 邻页没有图题 = 整页都是这一张图，取全部图形；
            # 否则按簇认领，避免与该页自己的图抢。
            solo = not any(a.kind == "figure" for a in nb.anchors)
            nb_box = _neighbor_latex_box(nb, step, None if solo else claimed)
            if nb_box is not None:
                claimed.add(("page", nb.pno))
                return _Placement(
                    anchor, nb.pno, finalize_bbox(nb, nb_box),
                    LayoutRoute.LATEX_VECTOR, Confidence.MEDIUM, [note],
                )

    # ④ 降级：整栏带 → 整页
    return _fallback_band(idx, anchor, info, col, ceiling, cap)


def _claim_raster(
    info: PageInfo, claimed: set, above_y: Optional[float], col: Optional[tuple[float, float]]
) -> Optional[fitz.Rect]:
    """认领一张尚未被占用的大位图。`above_y` 非空时只要 caption 上方的。"""
    if info.route is not LayoutRoute.JOURNAL_RASTER:
        return None
    page_area = _area(info.rect) or 1.0
    best: Optional[fitz.Rect] = None
    for r in info.img_rects:
        if _area(r) / page_area < JOURNAL_AREA_RATIO:
            continue
        key = (info.pno, int(r.x0), int(r.y0), int(r.x1), int(r.y1))
        if key in claimed:
            continue
        if above_y is not None and r.y1 > above_y + 2:
            continue
        if col is not None and not _shares_column(r, col):
            continue
        if best is None or r.y1 > best.y1:
            best = r
    if best is not None:
        claimed.add((info.pno, int(best.x0), int(best.y0), int(best.x1), int(best.y1)))
    return best


def _vertical_clusters(rects: list[fitz.Rect]) -> list[fitz.Rect]:
    """按垂直断裂把候选图形切成若干簇，自上而下返回各簇的并集。

    一页排两张图时（图题都在下一页）必须靠这个分开，否则两张图会各自取走整页，
    切出两张一模一样的图。实测 Nature Genetics 的 Fig. 6 与 Fig. 7 同在 p10。
    """
    if not rects:
        return []
    ordered = sorted(rects, key=lambda r: (r.y0, r.x0))
    clusters: list[fitz.Rect] = [fitz.Rect(ordered[0])]
    for r in ordered[1:]:
        if r.y0 - clusters[-1].y1 > CLUSTER_GAP:
            clusters.append(fitz.Rect(r))
        else:
            clusters[-1] |= r
    return clusters


def _neighbor_latex_box(
    nb: PageInfo, step: int, claimed: Optional[set] = None
) -> Optional[fitz.Rect]:
    """相邻页上的矢量图：上一页取末尾 caption 之下的图形，下一页取首个 caption 之上的。

    `claimed` 非空时逐簇认领——同一页被两张图共用时，第二张取下一簇而不是整页。
    """
    col = (nb.content_x0, nb.content_x1)
    if step == -1:
        lo = max([fitz.Rect(a.bbox).y1 for a in nb.anchors] or [nb.content_top])
        cands = [r for r in nb.img_rects + nb.draw_rects if r.y0 >= lo - 2]
    else:
        hi = min([fitz.Rect(a.bbox).y0 for a in nb.anchors] or [nb.content_bottom])
        cands = [r for r in nb.img_rects + nb.draw_rects if r.y1 <= hi + 2]
    if not cands:
        return None

    if claimed is None:
        box = fitz.Rect(cands[0])
        for r in cands[1:]:
            box |= r
    else:
        # 逐簇认领：上一张图取走的簇不再参与，避免两张图切出同一张整页。
        box = None
        for c in _vertical_clusters(cands):
            key = (nb.pno, int(c.x0), int(c.y0), int(c.x1), int(c.y1))
            if key in claimed:
                continue
            claimed.add(key)
            box = c
            break
        if box is None:
            return None

    box = fitz.Rect(box)
    box.x0 = max(box.x0, col[0] - 12)
    box.x1 = min(box.x1, col[1] + 12)
    return box if box.width > 8 and box.height > 40 else None


def _fallback_band(
    idx: DocIndex,
    anchor: CaptionAnchor,
    info: PageInfo,
    col: tuple[float, float],
    ceiling: float,
    cap: fitz.Rect,
) -> _Placement:
    """图形候选为空 → 切本栏 [ceiling, caption.y0] 整条带；带异常 → 切整页。

    页面整个没有图形时这一步是**必错**的：切出来的一定是正文
    （实测 Nature Genetics 的 Fig.5 因此切出一整段讨论文字）。
    此时如实标 LOW 并写明原因，让下游知道这张切图不能用，而不是假装切到了图。
    """
    if not _has_graphics(info):
        band = fitz.Rect(info.rect)
        return _Placement(
            anchor, info.pno, finalize_bbox(info, band, band, pad=2.0),
            LayoutRoute.FALLBACK_PAGE, Confidence.LOW,
            [f"p{info.page_no} 整页没有任何图形，未能定位到本图——"
             f"切图很可能是正文，请人工确认图实际在哪一页"],
        )

    lo_x, hi_x = column_limits(info, col)
    band = fitz.Rect(max(col[0] - 6, lo_x), ceiling, min(col[1] + 6, hi_x), cap.y0)
    ok = band.height >= BAND_MIN_H and band.height <= BAND_MAX_RATIO * info.rect.height
    if ok:
        return _Placement(
            anchor, info.pno, finalize_bbox(info, band, band, pad=2.0), LayoutRoute.FALLBACK_BAND,
            Confidence.MEDIUM, [f"未检出图形，降级切本栏整条带（{band.height:.0f}pt）"],
        )
    page_box = fitz.Rect(info.rect.x0, info.content_top, info.rect.x1, info.content_bottom)
    return _Placement(
        anchor, info.pno, page_box, LayoutRoute.FALLBACK_PAGE, Confidence.LOW,
        ["未检出图形且带高异常，降级切整页，需人工复核"],
    )


def _check_number_order(placements: list[_Placement]) -> None:
    """图号一致性校验：编号递增时页码不应倒退，否则多半配对串了。

    **必须按序列分别校验**——Extended Data 的编号从 1 重新开始且整体排在正文之后，
    混在一起查会把每一张 Extended Data 图都误报成「顺序异常」。
    """
    by_series: dict[str, list[tuple[int, _Placement]]] = {}
    for p in placements:
        if p.anchor.number.isdigit():
            by_series.setdefault(p.anchor.series, []).append((int(p.anchor.number), p))

    for series, numeric in by_series.items():
        numeric.sort(key=lambda t: t[0])
        head = _SERIES_DISPLAY.get(series, (series.title() + " ", ""))[0]
        prev_num, prev_page = None, None
        for num, p in numeric:
            if prev_page is not None and num != prev_num and p.pno < prev_page:
                p.warnings.append(
                    f"图号顺序异常：{head}Figure {num} 定位到 p{p.pno + 1}，"
                    f"早于 {head}Figure {prev_num} 的 p{prev_page + 1}"
                )
                p.confidence = Confidence.MEDIUM
            prev_num, prev_page = num, max(prev_page or 0, p.pno)


def _emit_assets(
    idx: DocIndex, placements: list[_Placement], out_dir: Path, dpi: int, kind: str,
) -> list[FigureAsset]:
    """按图号合并 `(continued)`，渲染切图，产出 FigureAsset。"""
    # 合并键必须含序列：图号只在同一序列内唯一，正文 Fig.1 与 Extended Data Fig.1
    # 是两张不同的图，只按图号分组会把它们并成一条（切图张冠李戴）。
    groups: dict[tuple[str, str], list[_Placement]] = {}
    order: list[tuple[str, str]] = []
    for p in placements:
        key = (p.anchor.series, p.anchor.number)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)

    assets: list[FigureAsset] = []
    fig_dir = out_dir / "figures"
    for key in order:
        series, num = key
        parts = sorted(groups[key], key=lambda p: (p.pno, p.box.y0))
        # 纯图题页只贡献文字。后缀按**可渲染**的段编号，否则会出现
        # 只有一张图却叫 fig_03_b 这种断号。
        renderable = [p for p in parts if not p.text_only] or parts
        primary = next(
            (p for p in renderable if not p.anchor.is_continued), renderable[0]
        )
        slug = figure_slug(kind, num, series)

        images: list[str] = []
        bboxes: list[BBox] = []
        pages: list[int] = []
        warns: list[str] = []
        routes: list[LayoutRoute] = []
        confs: list[Confidence] = []

        for i, part in enumerate(renderable):
            suffix = "" if len(renderable) == 1 else f"_{chr(ord('a') + i)}"
            dest = fig_dir / f"{slug}{suffix}.png"
            page = idx.pages[part.pno].page
            if not render_clip(page, part.box, dpi, dest):
                warns.append(f"p{part.pno + 1} 切图区域无效，已跳过")
                continue
            images.append(f"figures/{dest.name}")
            bboxes.append(_to_bbox(part.box))
            pages.append(part.pno + 1)
            warns.extend(part.warnings)
            routes.append(part.route)
            confs.append(part.confidence)

        if not images:
            warns.append("全部切图失败")
            routes = [primary.route]
            confs = [Confidence.LOW]
            pages = [primary.anchor.page]

        # 图题仍取自**全部**锚点——纯图题页那一条往往才是完整图题。
        caption = _merge_caption(parts, primary)
        if len(renderable) > 1:
            warns.append(f"跨页图：{len(renderable)} 段已合并为一条记录（pages={pages}）")
        if len(parts) > len(renderable):
            warns.append(
                f"图题另占 {len(parts) - len(renderable)} 页（p"
                + ", p".join(str(p.anchor.page) for p in parts if p.text_only)
                + "），已并入图题、不单独切图"
            )

        assets.append(
            FigureAsset(
                figure_id=_canonical_id(kind, num, series),
                kind=kind,  # type: ignore[arg-type]
                number=num,
                series=series,
                caption=caption,
                pages=sorted(set(pages)),
                images=images,
                bboxes=bboxes,
                layout_route=_worst_route(routes),
                bbox_confidence=_worst_conf(confs),
                warnings=warns,
            )
        )
    return assets


def _canonical_id(kind: str, number: str, series: str = "") -> str:
    """`Figure 3` / `Table 2` / `Extended Data Figure 1`。

    序列前缀参与身份，下游（context / 抽取 / 渲染）全部按 `figure_id` 索引，
    不带前缀就会和正文同号图撞车。
    """
    head = _SERIES_DISPLAY.get(series, (series.title() + " ", ""))[0]
    return f"{head}{'Figure' if kind == 'figure' else 'Table'} {number}"


def _merge_caption(parts: list[_Placement], primary: _Placement) -> str:
    """主图题为准；续页图题若有实质增量内容则追加。

    Nature 系整页大图常把图题挪走，占位处只写 `See next page for caption.`——
    此时改用同组里第一条实质图题作基底，否则下游拿到的「图题」是一句废话。
    """
    base = primary
    if PLACEHOLDER_CAPTION_RE.search(primary.anchor.text):
        base = next(
            (p for p in parts if not PLACEHOLDER_CAPTION_RE.search(p.anchor.text)),
            primary,
        )

    out = base.anchor.text
    for p in parts:
        if p is base or PLACEHOLDER_CAPTION_RE.search(p.anchor.text):
            continue
        extra = CONTINUED_RE.sub("", p.anchor.text[len(p.anchor.label):]).strip(" .:：")
        if len(extra) > 20:
            out = f"{out} {extra}"
    return out.strip()


_ROUTE_RANK = {
    LayoutRoute.JOURNAL_RASTER: 0,
    LayoutRoute.LATEX_VECTOR: 0,
    LayoutRoute.FALLBACK_BAND: 1,
    LayoutRoute.FALLBACK_PAGE: 2,
}
_CONF_RANK = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}


def _worst_route(routes: list[LayoutRoute]) -> LayoutRoute:
    """多段切图取最差的一段作为整条记录的路线标记。"""
    return max(routes, key=lambda r: _ROUTE_RANK.get(r, 0)) if routes else LayoutRoute.FALLBACK_PAGE


def _worst_conf(confs: list[Confidence]) -> Confidence:
    return max(confs, key=lambda c: _CONF_RANK[c]) if confs else Confidence.LOW
