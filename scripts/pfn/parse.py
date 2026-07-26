"""L1 · 解析总入口：PDF → `ParsedPaper`。

职责边界：**只做几何与文本提取，不做任何理解**。产出必须是确定性的、可复现的，
出错可定位可重跑（PLAN.md §3）。

    parse_pdf(pdf, out_dir) → ParsedPaper
        ├─ figures.build_figure_assets   切图 + 三级回退配对 + 跨页合并
        ├─ tables.build_table_assets     带状裁剪图 + 文本网格双通道
        └─ 本模块                        标题 / 章节树 / 段落

切图落在 `out_dir/figures/`，`FigureAsset.images` 存**相对 out_dir 的路径**。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import fitz

from pfn.figures import (
    CAPTION_RE,
    DocIndex,
    PageInfo,
    build_doc_index,
    build_figure_assets,
    norm_ws,
)
from pfn.models import (
    Confidence,
    FigureAsset,
    LayoutRoute,
    Paragraph,
    ParsedPaper,
    Section,
)
from pfn.tables import build_table_assets

#: 全文可提取字符少于此值判为扫描版（无文本层）。
MIN_TEXT_CHARS = 200
#: 首个标题之前的内容（标题页、摘要）挂在这个合成章节下。
FRONT_MATTER_ID = "§0"
#: 段落/标题判定阈值
MAX_HEADING_CHARS = 120
#: 标题最多折几行；再多就是正文
MAX_HEADING_LINES = 3
#: 连续多少行「加粗且写满整栏」就判为整段加粗的正文
BOLD_RUN_MIN = 3
SHORT_LINE_RATIO = 0.10
INDENT_PT = 4.0

_EMPHATIC_RE = re.compile(r"bold|black|medi|semib|heavy|\bbd\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2})*|[A-Z](?:\.\d{1,2})*)[.、]?\s+(?P<rest>\S.*)$")

#: 强 Methods 标记：其**子章节也算 Methods**（Nature 系把每个实验方法拆成二级标题）。
_METHODS_STRONG = re.compile(
    r"^(materials?\s*(and|&|与|和)\s*methods?|methods?\s*(and|&)\s*materials?"
    r"|methods?|methodology|材料\s*(与|和)\s*方法|实验\s*方法|方法)\b",
    re.IGNORECASE,
)
#: 弱 Methods 标记：只标记自身，不向下传染（避免 "3 Experiment" 把结果章节也染上）。
_METHODS_WEAK = re.compile(
    r"(experimental\s+(setup|settings?|details?|design|procedures?|section)"
    r"|experiments?\s+settings?|experiments?\s+setup|implementation\s+details?"
    r"|training\s+(setup|settings?|details?|config\w*)|model\s+configurations?"
    r"|data\s+(collection|acquisition)|datasets?\s+and\s+"
    r"|实验\s*(设置|设计|细节|配置)|实验部分|数据集)",
    re.IGNORECASE,
)


# ── 行流 ────────────────────────────────────────────────────────────────────


@dataclass
class _Line:
    page: int  # 1-based
    col: int
    rect: fitz.Rect
    text: str
    size: float
    emphatic: bool
    col_x1: float  # 本行所在栏的右边界，用于判断这一行有没有「写满」
    col_w: float
    #: 同一基线右侧还有非加粗文字接续 —— 这是「行内小标题」（ACL 的
    #: "**Evaluation Tasks** We conduct…"），不是独立章节标题。
    runs_in: bool = False
    #: 处在一长串「加粗且写满整栏」的行里 —— 这是整段加粗的正文，不是标题。
    bold_run: bool = False

    def fills_column(self) -> bool:
        return self.rect.x1 >= self.col_x1 - 0.02 * self.col_w


def _page_lines(page: fitz.Page, info: PageInfo) -> list[_Line]:
    """按「分栏 → 自上而下」的阅读顺序展开页面文本行，排除页眉页脚。

    必须做到行级：Nature 系把小标题和其后正文放进**同一个 block**，块级切分会漏掉标题。
    """
    lines: list[_Line] = []
    for blk in page.get_text("dict", textpage=info.textpage)["blocks"]:
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s["text"].strip()]
            if not spans:
                continue
            text = norm_ws("".join(s["text"] for s in spans))
            if not text:
                continue
            r = fitz.Rect(ln["bbox"])
            if r.y1 <= info.content_top or r.y0 >= info.content_bottom:
                continue
            cx = (r.x0 + r.x1) / 2
            col = 0
            for i, (c0, c1) in enumerate(info.columns):
                if c0 - 8 <= cx <= c1 + 8:
                    col = i
                    break
            else:
                col = 0 if cx < (info.content_x0 + info.content_x1) / 2 else len(info.columns) - 1
            emph = all(
                bool(s["flags"] & 2**4) or bool(_EMPHATIC_RE.search(s["font"])) for s in spans
            )
            c0, c1 = info.columns[min(col, len(info.columns) - 1)]
            lines.append(
                _Line(
                    page=info.page_no,
                    col=col,
                    rect=r,
                    text=text,
                    size=round(max(s["size"] for s in spans), 1),
                    emphatic=emph,
                    col_x1=c1,
                    col_w=max(1.0, c1 - c0),
                )
            )
    lines.sort(key=lambda l: (l.col, round(l.rect.y0, 1), l.rect.x0))
    _mark_run_in(lines)
    _mark_bold_runs(lines)
    return lines


def _mark_bold_runs(lines: list[_Line]) -> None:
    """标出整段加粗的正文。

    Nature 系的摘要通篇 Corbel-Bold，字号又和小标题重叠，样式上与标题完全无法区分。
    但标题至多一两行，正文段落是**连续一长串写满整栏的加粗行**——按连续游程长度切开。
    反过来，写满整栏但前后都是普通正文的孤立加粗行，仍然是标题（ACL 里就有几条
    刚好占满栏宽的小节标题），所以只有游程 ≥3 才判为正文。
    """
    run: list[_Line] = []

    def close() -> None:
        if len(run) >= BOLD_RUN_MIN:
            for l in run:
                l.bold_run = True
        run.clear()

    prev: Optional[_Line] = None
    for l in lines:
        cont = prev is not None and l.col == prev.col
        if l.emphatic and l.fills_column():
            if not cont:
                close()
            run.append(l)
        else:
            close()
        prev = l
    close()


def _mark_run_in(lines: list[_Line]) -> None:
    """标出同基线右侧被正文接续的加粗片段。"""
    for i, l in enumerate(lines):
        if not l.emphatic:
            continue
        for other in lines[i + 1 : i + 4]:
            if other.col != l.col or abs(other.rect.y0 - l.rect.y0) > 2.0:
                continue
            if not other.emphatic and l.rect.x1 - 1 <= other.rect.x0 <= l.rect.x1 + 30:
                l.runs_in = True
                break


def _body_size(lines: Iterable[_Line]) -> float:
    """正文字号 = 按字符数加权的众数。标题判定全靠它做基准。"""
    weight: dict[float, int] = {}
    for l in lines:
        weight[l.size] = weight.get(l.size, 0) + len(l.text)
    return max(weight, key=weight.get) if weight else 10.0


# ── 标题 ────────────────────────────────────────────────────────────────────


def _detect_title(lines: list[_Line], body: float) -> tuple[str, set[int]]:
    """首页最大字号的连续几行即论文标题。返回 (标题, 被占用的行 id)。"""
    first = [l for l in lines if l.page == lines[0].page] if lines else []
    if not first:
        return "", set()
    top = max(l.size for l in first)
    if top < body * 1.2:
        return "", set()
    picked = [l for l in first if l.size >= top - 0.2]
    picked.sort(key=lambda l: (l.rect.y0, l.rect.x0))
    return _join_wrapped(l.text for l in picked)[:400], {id(l) for l in picked}


def _join_wrapped(parts: Iterable[str]) -> str:
    """拼接折行文本：行尾连字符处不补空格（"DMC-" + "GF" → "DMC-GF"）。"""
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        out = p if not out else (out + p if out.endswith("-") else f"{out} {p}")
    return norm_ws(out)


# ── 章节树 ──────────────────────────────────────────────────────────────────


_SECTION_NO_RE = re.compile(r"^\d{1,2}(\.\d{1,2})*\.?$|^[A-Z](\.\d{1,2})*\.?$")


def _is_heading(line: _Line, body: float) -> bool:
    t = line.text
    if not (1 <= len(t) <= MAX_HEADING_CHARS):
        return False
    if CAPTION_RE.match(t) or line.runs_in or line.bold_run:
        return False
    if not (line.emphatic or line.size >= body + 1.5):
        return False
    if line.size < body - 0.3:
        return False
    if "&" in t or t.count(",") >= 2:  # 作者署名行
        return False
    if _SECTION_NO_RE.match(t):
        return True  # 独立成行的章节号，后面会与标题正文合并
    if sum(1 for c in t if c.isalpha() or "一" <= c <= "鿿") < 2:
        return False  # "···" 之类图内装饰
    if sum(c.isdigit() for c in t) > 0.5 * len(t):
        return False
    if t[-1] in ".;," and not _NUMBER_RE.match(t):
        return False
    return True


def _collect_headings(
    lines: list[_Line],
    body: float,
    used: set[int],
    blocked: dict[int, list[fitz.Rect]],
) -> list[list[_Line]]:
    """标题候选，并把折行的多行标题合并成一组。"""
    groups: list[list[_Line]] = []
    prev: Optional[_Line] = None
    for l in lines:
        if id(l) in used or _blocked(l, blocked.get(l.page, [])) or not _is_heading(l, body):
            prev = None
            continue
        starts_new = bool(_SECTION_NO_RE.match(l.text) or _NUMBER_RE.match(l.text))
        if (
            prev is not None
            and groups
            and not starts_new  # 紧挨着的 "2.1 …" / "2.1.1 …" 是两节，不能并成一条
            and l.page == prev.page
            and l.col == prev.col
            and abs(l.size - prev.size) <= 0.3
            and l.rect.y0 - prev.rect.y1 <= 1.5 * max(prev.rect.height, 1.0)
        ):
            groups[-1].append(l)
        else:
            groups.append([l])
        prev = l
    return [g for g in groups if _keep_group(g)]


def _keep_group(group: list[_Line]) -> bool:
    """剔除只剩章节号的残组，以及折行过多的（那不是标题，是正文）。"""
    text = norm_ws(" ".join(l.text for l in group))
    if sum(1 for c in text if c.isalpha() or "一" <= c <= "鿿") < 2:
        return False
    return len(group) <= MAX_HEADING_LINES


def _build_sections(groups: list[list[_Line]]) -> tuple[list[Section], dict[int, str]]:
    """标题组 → Section 列表，并返回「行 id → 所属章节 id」的归属表。"""
    if not groups:
        return [], {}

    # 只保留重复出现过的标题字号作为层级基准，一次性的大字号（刊名/OPEN 角标）丢弃
    sizes: dict[float, int] = {}
    for g in groups:
        sizes[g[0].size] = sizes.get(g[0].size, 0) + 1
    repeated = sorted((s for s, n in sizes.items() if n >= 2), reverse=True)
    if not repeated:
        repeated = sorted(sizes, reverse=True)
    level_of = {s: i + 1 for i, s in enumerate(repeated)}
    biggest = repeated[0]

    parsed = [(g, _NUMBER_RE.match(norm_ws(" ".join(l.text for l in g)))) for g in groups]
    numbered_doc = sum(1 for _, m in parsed if m) >= 0.4 * len(parsed)

    sections: list[Section] = []
    counters: list[int] = []
    owner: dict[int, str] = {}
    taken: set[str] = {FRONT_MATTER_ID}
    last_numbered = ""
    sub = 0
    for g, m in parsed:
        size = g[0].size
        if size not in level_of and size > biggest:
            continue  # 孤立的超大字号，不是章节
        text = norm_ws(" ".join(l.text for l in g))
        if m:  # 原文自带编号（"3.1 Experiment Settings" / "A.8 …"）直接沿用
            num, title = m.group("num").rstrip("."), m.group("rest")
            level = num.count(".") + 1
            sid = f"§{num}"
            last_numbered, sub = num, 0
        elif numbered_doc:
            # 编号体系里的无编号标题（Abstract、附录里的小标题）挂到上一节之下，
            # 这样绝不会和原文编号撞车
            sub += 1
            parent = last_numbered or "0"
            sid = f"§{parent}.{sub}"
            level = parent.count(".") + 2
            title = text
        else:
            level = level_of.get(size, min(level_of.values(), default=1))
            level = min(level, len(counters) + 1)  # 跳级时向上贴齐，避免出现 §1.0.1
            counters = counters[:level]
            while len(counters) < level:
                counters.append(0)
            counters[level - 1] += 1
            sid = "§" + ".".join(str(c) for c in counters)
            title = text
        sid = _unique(sid, taken)
        sections.append(
            Section(
                id=sid,
                title=title[:200],
                level=level,
                page_start=g[0].page,
                is_methods=False,
            )
        )
        for l in g:
            owner[id(l)] = sid
    _mark_methods(sections)
    return sections, owner


def _unique(sid: str, taken: set[str]) -> str:
    """章节 id 是段落溯源的主键，必须唯一。"""
    out, n = sid, 1
    while out in taken:
        n += 1
        out = f"{sid}#{n}"
    taken.add(out)
    return out


def _mark_methods(sections: list[Section]) -> None:
    """标记 Methods 章节。强标记向下传染到子章节，弱标记只管自己。"""
    strong_level: Optional[int] = None
    for s in sections:
        if strong_level is not None and s.level > strong_level:
            s.is_methods = True
            continue
        strong_level = None
        if _METHODS_STRONG.match(s.title):
            s.is_methods = True
            strong_level = s.level
        elif _METHODS_WEAK.search(s.title):
            s.is_methods = True


# ── 段落 ────────────────────────────────────────────────────────────────────


def _build_paragraphs(
    lines: list[_Line],
    infos: dict[int, PageInfo],
    sections: list[Section],
    owner: dict[int, str],
    used: set[int],
    blocked: dict[int, list[fitz.Rect]],
) -> list[Paragraph]:
    """把正文行聚成段落，id 形如 `§3.2¶2`。

    切段信号（按可靠性排序）：换页/换栏 → 上一行「未写满」→ 本行首行缩进 → 行距突增。
    Nature 系整节正文只有一个 block，只能靠这几个信号切；ACL 的 block 又会被行内公式
    切碎，所以刻意**不用** block 边界。
    """
    heights = [l.rect.height for l in lines if l.rect.height > 0]
    line_h = statistics.median(heights) if heights else 10.0

    paragraphs: list[Paragraph] = []
    counters: dict[str, int] = {}
    section_id = FRONT_MATTER_ID  # 首个标题之前的内容（摘要等）
    buf: list[_Line] = []
    prev: Optional[_Line] = None

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        text = norm_ws(" ".join(l.text for l in buf))
        if len(text) >= 2:
            n = counters.get(section_id, 0) + 1
            counters[section_id] = n
            paragraphs.append(
                Paragraph(
                    id=f"{section_id}¶{n}",
                    section_id=section_id,
                    page=buf[0].page,
                    text=text,
                )
            )
        buf = []

    for l in lines:
        if id(l) in owner:  # 标题行本身不进段落
            flush()
            section_id = owner[id(l)]
            prev = None
            continue
        if id(l) in used:  # 论文标题
            continue
        if _blocked(l, blocked.get(l.page, [])):
            flush()
            prev = None
            continue

        info = infos.get(l.page - 1)
        if prev is not None and info is not None and _breaks(prev, l, info, line_h):
            flush()
        buf.append(l)
        prev = l

    flush()
    if any(p.section_id == FRONT_MATTER_ID for p in paragraphs):
        sections.insert(
            0,
            Section(
                id=FRONT_MATTER_ID,
                title="Front matter",
                level=1,
                page_start=paragraphs[0].page,
            ),
        )
    return paragraphs


def _blocked(line: _Line, rects: list[fitz.Rect]) -> bool:
    """落在图题或切图区域内的行不算正文（坐标轴刻度、图例会污染段落）。"""
    area = line.rect.get_area()
    if area <= 0:
        return False
    return any((line.rect & r).get_area() > 0.6 * area for r in rects)


def _breaks(prev: _Line, cur: _Line, info: PageInfo, line_h: float) -> bool:
    if cur.page != prev.page or cur.col != prev.col:
        return True
    # 同一基线上的碎片（行内公式被 PyMuPDF 拆成独立 line）永远算续行
    share = min(prev.rect.y1, cur.rect.y1) - max(prev.rect.y0, cur.rect.y0)
    if share > 0.5 * min(prev.rect.height, cur.rect.height):
        return False
    c0, c1 = info.columns[min(cur.col, len(info.columns) - 1)]
    width = max(1.0, c1 - c0)
    if prev.rect.x1 < c1 - SHORT_LINE_RATIO * width:  # 上一行没写满 = 段落结束
        return True
    if cur.rect.x0 > c0 + INDENT_PT:  # 首行缩进
        return True
    if cur.rect.y0 - prev.rect.y1 > 1.2 * line_h:  # 行距突增
        return True
    return False


def _blocked_rects(
    idx: DocIndex, assets: list[FigureAsset]
) -> dict[int, list[fitz.Rect]]:
    """每页需要从正文里排除的矩形：所有图题/表题块 + 非降级的切图区域。"""
    out: dict[int, list[fitz.Rect]] = {}
    for pno in idx.order:
        info = idx.pages[pno]
        out.setdefault(info.page_no, []).extend(fitz.Rect(a.bbox) for a in info.anchors)
    for a in assets:
        if a.layout_route is LayoutRoute.FALLBACK_PAGE:
            continue  # 整页兜底，排除它等于丢掉全页正文
        if len(a.pages) != len(a.bboxes):
            # pages 去过重、bboxes 对齐的是 images，长度不等时无法确定对应关系；
            # 与其把某页的切图框安到别页去误删正文，不如只靠图题框兜着。
            continue
        for page, box in zip(a.pages, a.bboxes):
            out.setdefault(page, []).append(fitz.Rect(box))
    return out


# ── 入口 ────────────────────────────────────────────────────────────────────


def parse_pdf(
    pdf: Path, out_dir: Path, dpi: int = 200, pages: list[int] | None = None
) -> ParsedPaper:
    """PDF → 结构化内容 + 切图。任何一步失败都降级兜底，不抛异常中断。"""
    pdf = Path(pdf)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf)
    try:
        n_pages = len(doc)
        page_ids = _select_pages(n_pages, pages)
        parsed = ParsedPaper(pdf_path=str(pdf), n_pages=n_pages)
        if not page_ids:
            parsed.has_text_layer = False
            parsed.warnings.append("--pages 选出的页范围为空")
            return parsed

        idx = build_doc_index(doc, page_ids)
        parsed.warnings.extend(idx.warnings)

        if idx.total_chars < MIN_TEXT_CHARS:
            parsed.has_text_layer = False
            parsed.warnings.append(
                f"全文仅提取到 {idx.total_chars} 个字符，判为扫描版（无文本层）"
            )
            return parsed

        assets: list[FigureAsset] = []
        assets.extend(_safe(lambda: build_figure_assets(idx, out_dir, dpi), parsed, "图切割"))
        assets.extend(_safe(lambda: build_table_assets(idx, out_dir, dpi), parsed, "表抽取"))

        lines: list[_Line] = []
        for pno in idx.order:
            info = idx.pages[pno]
            lines.extend(_page_lines(info.page, info))
        lines.sort(key=lambda l: (l.page, l.col, round(l.rect.y0, 1), l.rect.x0))

        body = _body_size(lines)
        title, title_ids = _detect_title(lines, body)
        blocked = _blocked_rects(idx, assets)
        groups = _collect_headings(lines, body, title_ids, blocked)
        sections, owner = _build_sections(groups)
        paragraphs = _build_paragraphs(
            lines, idx.pages, sections, owner, title_ids, blocked
        )

        parsed.title = title
        parsed.sections = sections
        parsed.paragraphs = paragraphs
        parsed.assets = _sort_assets(assets)
        parsed.warnings.extend(_asset_warnings(parsed.assets))
        return parsed
    finally:
        doc.close()


def _select_pages(n_pages: int, pages: list[int] | None) -> list[int]:
    """`pages` 是 1-based 页码列表；返回 0-based 且已去重排序的有效页。"""
    if not pages:
        return list(range(n_pages))
    return sorted({p - 1 for p in pages if 1 <= p <= n_pages})


def _safe(fn, parsed: ParsedPaper, what: str) -> list[FigureAsset]:
    """任一环节炸了也要有产出——记 warning 后继续，绝不中断整篇解析。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — 兜底就是要吞掉一切
        parsed.warnings.append(f"{what}失败并已跳过：{type(exc).__name__}: {exc}")
        return []


def _sort_assets(assets: list[FigureAsset]) -> list[FigureAsset]:
    def key(a: FigureAsset):
        try:
            num = int(a.number)
        except ValueError:
            num = 999
        return (0 if a.kind == "figure" else 1, num, a.number)

    return sorted(assets, key=key)


def _asset_warnings(assets: list[FigureAsset]) -> list[str]:
    out: list[str] = []
    for a in assets:
        if a.bbox_confidence is not Confidence.HIGH:
            out.append(
                f"{a.figure_id} 切图置信度 {a.bbox_confidence.value}（{a.layout_route.value}）"
            )
        if not a.images:
            out.append(f"{a.figure_id} 没有产出任何切图")
    return out
