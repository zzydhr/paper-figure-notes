"""L1 · 表格双通道抽取。

几何法定位表体已在 `references/feasibility.md` §3 证伪，`page.find_tables()` 在三线表上
同样不可用（Table 1 检出 0 个、Table 2 解析成 3×2 乱码）。因此这里走**双通道**：

* **通道 A（图像）**：裁剪本栏内 [上一个 caption 底边, 下一个 caption 顶边] 的带状区域渲染成图。
  宁可多切空白，也不会切错内容。VLM 读三线表图像很稳。
* **通道 B（文本）**：同区域用 `page.get_text("words")` 按 y 聚行、按 x 聚列还原成
  `table_text_grid`，给模型提供**精确数字串**做交叉校验，防止读图看错小数点。

表题在表上方还是下方**因刊而异**（实测 ACL 这篇全部在下方），所以带的方向不写死，
按「哪一侧更像表」自动选：表线数 > 数字密度 > 文本行数。
"""

from __future__ import annotations

import re
import statistics
from bisect import bisect
from pathlib import Path
from typing import Optional

import fitz

from pfn.models import BBox, CaptionAnchor, Confidence, FigureAsset, LayoutRoute

from pfn.figures import (
    DocIndex,
    PageInfo,
    _overlap_1d,
    _to_bbox,
    caption_column,
    ceiling_for,
    figure_slug,
    finalize_bbox,
    floor_for,
    norm_ws,
    render_clip,
)

#: 判为「表线」的横线最小相对栏宽。三线表的顶/中/底线都横贯整栏。
RULE_MIN_COL_FRAC = 0.40
#: 带高的合理区间
BAND_MIN_H = 24.0
BAND_MAX_RATIO = 0.85
#: 文本网格规模上限，避免病态输出撑爆 JSON
GRID_MAX_ROWS = 250
GRID_MAX_COLS = 40

_NUMERIC_RE = re.compile(r"^[-+±(\[]?\d[\d,.]*\)?%?[*†‡]*$")


def build_table_assets(idx: DocIndex, out_dir: Path, dpi: int) -> list[FigureAsset]:
    """表题定位 → 选带 → 收紧到表线 → 切图 + 还原文本网格。"""
    anchors: list[tuple[PageInfo, CaptionAnchor]] = []
    for pno in idx.order:
        info = idx.pages[pno]
        anchors.extend((info, a) for a in info.anchors if a.kind == "table")
    anchors.sort(key=lambda t: (t[1].page, t[1].bbox[1], t[1].bbox[0]))

    side = vote_caption_side(anchors)
    # 同号表出现多次 = 跨页续表，切图必须分文件名，否则后一段会覆盖前一段
    total: dict[str, int] = {}
    for _, a in anchors:
        total[a.number] = total.get(a.number, 0) + 1

    assets: list[FigureAsset] = []
    seen: dict[str, int] = {}
    for info, anchor in anchors:
        n = seen.get(anchor.number, 0)
        seen[anchor.number] = n + 1
        suffix = "" if total[anchor.number] == 1 else f"_{chr(ord('a') + n)}"
        assets.append(_build_one(idx, info, anchor, side, out_dir, dpi, suffix))
    return _merge_continued(assets)


def _build_one(
    idx: DocIndex,
    info: PageInfo,
    anchor: CaptionAnchor,
    side: str,
    out_dir: Path,
    dpi: int,
    suffix: str = "",
) -> FigureAsset:
    page = info.page
    cap = fitz.Rect(anchor.bbox)
    col = caption_column(info, cap)
    warnings: list[str] = []

    band, tightened = _choose_band(info, cap, col, side)
    if band is None:
        band = fitz.Rect(info.rect.x0, info.content_top, info.rect.x1, info.content_bottom)
        route, conf = LayoutRoute.FALLBACK_PAGE, Confidence.LOW
        warnings.append("表体带定位失败，降级切整页，需人工复核")
    elif tightened:
        # 命中表线 = 拿到了表体真实上下边界，这是正常路线而非降级
        route, conf = info.route, Confidence.HIGH
        warnings.append(f"表体按表线收紧到表题{side}方（{band.height:.0f}pt）")
    else:
        route, conf = LayoutRoute.FALLBACK_BAND, Confidence.MEDIUM
        warnings.append(f"未检出表线，降级切表题{side}方整条带（{band.height:.0f}pt）")

    box = finalize_bbox(info, band, band, pad=3.0)
    slug = figure_slug("table", anchor.number)
    dest = out_dir / "figures" / f"{slug}{suffix}.png"
    images: list[str] = []
    bboxes: list[BBox] = []
    if render_clip(page, box, dpi, dest):
        images.append(f"figures/{dest.name}")
        bboxes.append(_to_bbox(box))
    else:
        warnings.append("切图区域无效")
        conf = Confidence.LOW

    grid = words_to_grid(info.words, box)
    if not grid:
        warnings.append("文本网格还原为空（可能是图片表）")

    return FigureAsset(
        figure_id=f"Table {anchor.number}",
        kind="table",
        number=anchor.number,
        caption=anchor.text,
        pages=[anchor.page],
        images=images,
        bboxes=bboxes,
        layout_route=route,
        bbox_confidence=conf,
        table_text_grid=grid or None,
        warnings=warnings,
    )


# ── 通道 A：带状区域定位 ────────────────────────────────────────────────────


def vote_caption_side(anchors: list[tuple[PageInfo, CaptionAnchor]]) -> str:
    """全文投票：表题在表的「上」还是「下」。

    **必须全文统一表决，不能逐表判断。** 表格连排时，第 N 张表的「下方带」和
    第 N+1 张表的「上方带」是同一段区域，逐表打分必然有一半选反（实测 ACL 的
    Table 4/8/12 全选反了）。

    判据是**贴合度**而非内容量：表体紧挨自己的表题，另一侧的表格总在更远处。
    实测这篇 ACL 论文 19 张表里，本侧表线间距一律 ~10pt，另一侧 ≥25pt 或没有，
    信号极其干净。
    """
    above = below = 0
    for info, a in anchors:
        cap = fitz.Rect(a.bbox)
        col = caption_column(info, cap)
        ga = _nearest_rule_gap(info, col, ceiling_for(info, cap, col), cap.y0, "上")
        gb = _nearest_rule_gap(info, col, cap.y1, floor_for(info, cap, col), "下")
        if ga is None and gb is None:
            continue
        if gb is None or (ga is not None and ga <= gb):
            above += 1
        else:
            below += 1
    if above == below == 0:
        return _vote_by_density(anchors)
    return "上" if above >= below else "下"


def _vote_by_density(anchors: list[tuple[PageInfo, CaptionAnchor]]) -> str:
    """全文一条表线都没有（无框线表）时的退路：比两侧的数字密度。"""
    above = below = 0
    for info, a in anchors:
        cap = fitz.Rect(a.bbox)
        col = caption_column(info, cap)
        up = fitz.Rect(col[0] - 6, ceiling_for(info, cap, col), col[1] + 6, cap.y0)
        dn = fitz.Rect(col[0] - 6, cap.y1, col[1] + 6, floor_for(info, cap, col))
        su = _band_score(info, up, col)[0] if _band_sane(up, info) else -1.0
        sd = _band_score(info, dn, col)[0] if _band_sane(dn, info) else -1.0
        if su >= sd:
            above += 1
        else:
            below += 1
    return "上" if above >= below else "下"


def _nearest_rule_gap(
    info: PageInfo, col: tuple[float, float], lo: float, hi: float, side: str
) -> Optional[float]:
    """本侧最近一条表线与表题之间的空隙；本侧无表线返回 None。"""
    rules = _rules_in(info, fitz.Rect(col[0] - 6, lo, col[1] + 6, hi), col)
    if not rules:
        return None
    return (hi - max(r.y1 for r in rules)) if side == "上" else (min(r.y0 for r in rules) - lo)


def _choose_band(
    info: PageInfo, cap: fitz.Rect, col: tuple[float, float], side: str
) -> tuple[Optional[fitz.Rect], bool]:
    """在全文表决出的那一侧取带，命中表线则收紧。返回 (带, 是否已收紧)。"""
    ceiling = ceiling_for(info, cap, col)
    floor = floor_for(info, cap, col)
    band = (
        fitz.Rect(col[0] - 6, ceiling, col[1] + 6, cap.y0)
        if side == "上"
        else fitz.Rect(col[0] - 6, cap.y1, col[1] + 6, floor)
    )
    if not _band_sane(band, info):
        return None, False

    rules = _rules_in(info, band, col)
    if not rules:
        return band, False

    # 表线是三线表的真实边界：纵向据此剔除带里夹杂的正文段落，
    # 横向直接用表线的跨度——比栏边界准，图表页上的栏检测本来就不可信。
    lo = min(r.y0 for r in rules) - 8
    hi = max(r.y1 for r in rules) + 8
    x0 = min(min(r.x0 for r in rules) - 6, band.x0)
    x1 = max(max(r.x1 for r in rules) + 6, band.x1)
    if side == "上":
        return fitz.Rect(x0, max(band.y0, lo), x1, band.y1), True
    return fitz.Rect(x0, band.y0, x1, min(band.y1, hi)), True


def _rules_in(
    info: PageInfo, band: fitz.Rect, col: tuple[float, float]
) -> list[fitz.Rect]:
    col_w = max(1.0, col[1] - col[0])
    return [
        r
        for r in info.hrules
        if band.y0 - 2 <= r.y0 and r.y1 <= band.y1 + 2
        and _overlap_1d(r.x0, r.x1, col[0], col[1]) > RULE_MIN_COL_FRAC * col_w
    ]


def _band_sane(band: fitz.Rect, info: PageInfo) -> bool:
    return (
        band.height >= BAND_MIN_H
        and band.height <= BAND_MAX_RATIO * info.rect.height
        and band.width > 20
    )


def _band_score(
    info: PageInfo, band: fitz.Rect, col: tuple[float, float]
) -> tuple[float, list[fitz.Rect]]:
    """表格特征打分：表线 > 数字密度 > 文本行数。"""
    rules = _rules_in(info, band, col)
    words = _words_in(info, band, col)
    n_words = len(words)
    numeric = sum(1 for w in words if _NUMERIC_RE.match(w[4]))
    density = numeric / n_words if n_words else 0.0
    n_lines = len({round(w[1] / 4) for w in words})
    return 3.0 * len(rules) + 12.0 * density + 0.4 * min(n_lines, 25), rules


def _words_in(info: PageInfo, band: fitz.Rect, col: tuple[float, float]) -> list[tuple]:
    return [
        w
        for w in info.words
        if band.y0 - 1 <= (w[1] + w[3]) / 2 <= band.y1 + 1
        and col[0] - 12 <= (w[0] + w[2]) / 2 <= col[1] + 12
    ]


# ── 通道 B：文本网格还原 ────────────────────────────────────────────────────


def words_to_grid(page_words: list[tuple], box: fitz.Rect) -> list[list[str]]:
    """把区域内的 words 按 y 聚行、按 x 投影切列，还原成文本网格。

    列边界不靠「行内间隙超过某阈值」去猜——那个阈值在 `35.2 5.1` 这种紧挨着的
    数字列上必然失手。改成看**整块区域的 x 投影**：表格的列天生纵向对齐，
    列与列之间存在一条贯穿几乎所有行的空白通道，找这些通道即可。
    """
    words = [
        w for w in page_words if fitz.Rect(w[:4]).intersects(box) and w[4].strip()
    ]
    if not words:
        return []

    heights = [w[3] - w[1] for w in words if w[3] > w[1]]
    line_h = statistics.median(heights) if heights else 10.0
    row_tol = max(2.0, 0.55 * line_h)

    rows: list[list[tuple]] = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        yc = (w[1] + w[3]) / 2
        if rows and abs(yc - _row_center(rows[-1])) <= row_tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    rows = rows[:GRID_MAX_ROWS]

    bounds = _column_bounds(rows, box)
    grid: list[list[str]] = []
    for row in rows:
        cells: list[list[str]] = [[] for _ in range(len(bounds) - 1)]
        for w in sorted(row, key=lambda w: w[0]):
            xc = (w[0] + w[2]) / 2
            j = max(0, min(len(cells) - 1, bisect(bounds, xc) - 1))
            cells[j].append(w[4])
        line = [norm_ws(" ".join(c)) for c in cells]
        if any(line):
            grid.append(line)
    return grid


def _row_center(row: list[tuple]) -> float:
    return sum((w[1] + w[3]) / 2 for w in row) / len(row)


def _column_bounds(rows: list[list[tuple]], box: fitz.Rect) -> list[float]:
    """x 投影找列间空白通道，返回列边界（含左右两端）。

    允许通道被少量行穿过——`Generation` 这类跨列表头本来就会盖住下面几列的通道，
    按「被超过 1/4 的行占用才算不是通道」放行即可。
    """
    n = max(1, int(box.width) + 2)
    cover = [0] * n
    for row in rows:
        marked: set[int] = set()
        for w in row:
            a = max(0, int(w[0] - box.x0))
            b = min(n - 1, int(w[2] - box.x0))
            marked.update(range(a, b + 1))
        for i in marked:
            cover[i] += 1

    thr = max(1, int(0.25 * len(rows)))
    runs: list[tuple[int, int]] = []
    start: Optional[int] = None
    for i, c in enumerate(cover):
        if c <= thr:
            start = i if start is None else start
        elif start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, n))

    inner = [r for r in runs if r[0] > 0 and r[1] < n and r[1] - r[0] >= 2]
    if len(inner) > GRID_MAX_COLS - 1:  # 通道太多，只留最宽的几条
        inner = sorted(sorted(inner, key=lambda r: r[0] - r[1])[: GRID_MAX_COLS - 1])
    cuts = [box.x0 + (a + b) / 2 for a, b in inner]
    return [box.x0 - 1] + cuts + [box.x1 + 1]


# ── 跨页表合并 ──────────────────────────────────────────────────────────────


def _merge_continued(assets: list[FigureAsset]) -> list[FigureAsset]:
    """`Table N (continued)` 与主表归为一条，与图的处理保持一致。"""
    merged: dict[str, FigureAsset] = {}
    order: list[str] = []
    for a in assets:
        if a.number not in merged:
            merged[a.number] = a
            order.append(a.number)
            continue
        head = merged[a.number]
        head.pages = sorted(set(head.pages + a.pages))
        head.images.extend(a.images)
        head.bboxes.extend(a.bboxes)
        head.warnings.extend(a.warnings)
        head.warnings.append(f"跨页表：合并 p{a.pages} 的续表")
        if a.table_text_grid:
            head.table_text_grid = (head.table_text_grid or []) + a.table_text_grid
    return [merged[n] for n in order]
