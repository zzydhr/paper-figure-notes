"""L2 装配层 —— 为每张图/表凑齐做实验设计抽取所需的全部原文上下文。

输入 `ParsedPaper`（L1 产出，只读），输出 `ContextPack[]`，同时把每个 pack 渲染成
人可读的 `<out_dir>/context/<stem>.md`，用于排查「模型为什么抽错」——
一眼就能看出喂给模型的原文是否齐全。

三件事（见 PLAN.md §3「L2 关键实现」）：

    1. 正文交叉引用扫描  找出所有引用本图的段落，整段收进来，保留 §id
    2. Methods 关联召回  从图题 + 引用段抽方法学关键词，回 Methods 章节打分召回 top-k
    3. 子图描述切分      从图题里切出 (A)/(a)/A. 各 panel 的描述

本层是纯确定性的：同样的 `parsed.json` 必得同样的 context，出错可定位可复跑。
任何一图装配失败都不会中断整体，失败图退化为「只有图题」的 pack 并记 warning。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from pfn.models import (
    Confidence,
    ContextPack,
    FigureAsset,
    Paragraph,
    ParsedPaper,
    Section,
)

# ── 可调参数 ────────────────────────────────────────────────────────────────

#: 一张图最多收几段正文引用（超出只保留文档序靠前的，其余在诊断里列出）
MAX_CITING_PARAGRAPHS = 12
#: Methods 召回条数
METHODS_TOP_K = 5
#: Methods 段落入选的最低分（≈ 至少命中一个实体级关键词）
MIN_METHODS_SCORE = 1.0
#: 没有任何关键词命中时，兜底取 Methods 前 N 段（聊胜于无，md 里会标注）
METHODS_FALLBACK_N = 2
#: 参与召回打分的关键词上限，防止长引用段把关键词稀释成噪音
MAX_KEYWORDS = 40
#: 子图标号后至少要跟这么多字符的描述，才认为是真的 panel 标号而非句中的 "(A)"
MIN_PANEL_DESC_CHARS = 10
#: 引用范围 `Figures 1-N` 的最大展开长度，防病态输入
MAX_RANGE_SPAN = 20


# ══════════════════════════════════════════════════════════════════════════
# 一、正文交叉引用扫描
# ══════════════════════════════════════════════════════════════════════════

# LaTeX 排版的 PDF 里 `Figure~1` 的连接符会抽成 `~` 或不换行空格，一并当空白
_SP = r"[ \t ~]*"

#: 英文引导词。`\b` + 可选点号，覆盖 Fig / Fig. / Figs. / Figure / FIGURE / Table / Tab.
_EN_LEAD_RE = re.compile(
    r"\b(?P<sup>(?:supplementary|supplemental|supp|extended[ \t]+data|ext\.?[ \t]+data)[ \t]+)?"
    r"(?P<word>figures|figure|figs|fig|tables|table|tabs|tab|tbls|tbl)\b\.?" + _SP,
    re.IGNORECASE,
)

#: 中文引导词。前面若是「列/图/标/该/本」等字则不算引用（避开「列表 3 个」「图表 1」）
_CN_LEAD_RE = re.compile(
    r"(?<![列图标该本此其每两三四五六七八九十])(?P<sup>附|补充)?(?P<word>[图表])" + _SP
)

_PLURAL_WORDS = {"figures", "figs", "tables", "tabs", "tbls"}

#: 数字项：`1` / `S1` / `S 1` / `ED2`（补充材料）
_INT_ITEM_RE = re.compile(r"(?P<pre>[SEse][Dd]?)?[ \t]?(?P<digits>\d{1,3})(?![\d.]*\d*[%°])")
#: 罗马数字（Table IV 这类），只在表格且首项时启用
_ROMAN_ITEM_RE = re.compile(r"(?P<roman>[IVXLC]{1,6})(?![A-Za-z])")

#: 子图标号：`(A)` `(a-c)` `(A, B)` 或紧跟数字的 `A` `a-c`
_PAREN_PANEL_RE = re.compile(
    rf"[ \t]?\({_SP}(?P<body>[A-Za-z](?:{_SP}(?:[-–—,、]|and|&)?{_SP}[A-Za-z])*){_SP}\)"
)
_BARE_PANEL_RE = re.compile(
    rf"(?P<gap>[ \t]?)(?P<body>[A-Za-z](?![A-Za-z])(?:{_SP}[-–—]{_SP}[A-Za-z](?![A-Za-z]))?)"
)

#: 项与项之间的分隔符。顺序重要：范围 → and/和 → 逗号
_SEP_RANGE_RE = re.compile(r"[ \t]*[-–—~‐][ \t]*")
_SEP_AND_RE = re.compile(
    r"[ \t]*(?:,[ \t]*)?(?:and|&|or|as[ \t]+well[ \t]+as|、|和|与|及|以及)[ \t]*", re.IGNORECASE
)
_SEP_COMMA_RE = re.compile(r"[ \t]*[,;、][ \t]*")

#: 单位/量词表。数字后跟这些词说明它是剂量或时长，不是图号（`Fig. 1, 2 h after ...`）
_UNIT_WORDS = {
    # 时间
    "h", "hr", "hrs", "hour", "hours", "min", "mins", "minute", "minutes",
    "s", "sec", "secs", "second", "seconds", "d", "day", "days",
    "w", "wk", "wks", "week", "weeks", "month", "months", "year", "years",
    # 长度 / 质量 / 体积 / 浓度
    "m", "mm", "cm", "nm", "um", "μm", "µm", "g", "mg", "kg", "ng", "ug", "μg", "µg",
    "l", "ml", "μl", "µl", "ul", "mol", "mmol", "μmol", "µmol", "nmol",
    "mM", "μM", "µM", "nM", "pM",
    # 其它
    "x", "×", "fold", "times", "c", "°c", "gy", "hz", "bp", "kb", "kda", "rpm", "v",
    # 量词
    "cells", "cell", "mice", "mouse", "rats", "rat", "patients", "samples", "sample",
    "replicates", "wells", "well", "groups", "group", "runs", "seeds", "epochs",
    "layers", "heads", "tokens", "points", "experiments", "sections", "slices",
}

_ROMAN_MAP = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _roman_to_int(text: str) -> Optional[int]:
    """`IV` → 4。非合法罗马数字返回 None。"""
    s = text.strip().upper()
    if not s or any(ch not in _ROMAN_MAP for ch in s):
        return None
    total, prev = 0, 0
    for ch in reversed(s):
        val = _ROMAN_MAP[ch]
        total += -val if val < prev else val
        prev = max(prev, val)
    return total or None


def norm_number(raw: str) -> str:
    """图号归一化，作为「引用 ↔ asset」的匹配键。

    `01` / `1` → `1`；`S 2` → `S2`；`IV` → `4`（罗马与阿拉伯统一，同一篇不会混用两套编号）。
    """
    s = (raw or "").strip().upper().replace(" ", "").replace(" ", "")
    m = re.fullmatch(r"([SE]D?)?0*(\d+)", s)
    if m:
        return f"{m.group(1) or ''}{int(m.group(2))}"
    r = _roman_to_int(s)
    if r is not None:
        return str(r)
    return s


@dataclass
class FigureRef:
    """正文里的一处图号引用。"""

    kind: str  # "figure" | "table"
    number: str  # 归一化后的图号，如 "1" / "S2"
    panels: list[str] = field(default_factory=list)  # 子图标号（大写），如 ["A", "B"]
    raw: str = ""  # 原文片段，如 "Figs. 1 and 2"
    start: int = 0
    end: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.number)


def _looks_like_unit(text: str, pos: int) -> bool:
    """`pos` 处开头的 token 是不是单位/量词（用来否掉「2 h」这种伪图号）。"""
    m = re.match(r"[ \t ]*(%|[A-Za-zμµ°]{1,10})", text[pos : pos + 16])
    if not m:
        return False
    tok = m.group(1)
    if tok == "%":
        return True
    return tok.strip("°").lower() in _UNIT_WORDS or tok in _UNIT_WORDS


def _parse_panels(body: str) -> list[str]:
    """`A-C` → [A,B,C]；`A, B` → [A,B]；`see A` → []（含非单字母 token 一律判为误匹配）。"""
    parts = [p for p in re.split(r"[ \t]*(?:[-–—,、&]|and|und|与|和)[ \t]*", body, flags=re.I) if p]
    if not parts or any(len(p) != 1 or not p.isalpha() for p in parts):
        return []
    letters = [p.upper() for p in parts]
    is_range = bool(re.search(r"[-–—]", body)) and len(letters) == 2
    if is_range:
        a, b = ord(letters[0]), ord(letters[1])
        if 0 < b - a <= 12:
            return [chr(c) for c in range(a, b + 1)]
        return []
    if len(set(letters)) != len(letters) or len(letters) > 8:
        return []
    return letters


def _match_panel(text: str, pos: int) -> tuple[list[str], int]:
    """从 `pos` 开始尝试吃掉一个子图标号，返回 (标号列表, 新位置)。"""
    m = _PAREN_PANEL_RE.match(text, pos)
    if m:
        panels = _parse_panels(m.group("body"))
        if panels:
            return panels, m.end()
    m = _BARE_PANEL_RE.match(text, pos)
    if m:
        # 「数字与字母之间有空格」时要防单位误判：`Fig. 1 d` 是 1 天，`Fig. 1d` 才是子图 d
        if m.group("gap") and _looks_like_unit(text, pos):
            return [], pos
        panels = _parse_panels(m.group("body"))
        if panels:
            return panels, m.end()
    return [], pos


def _match_item(
    text: str, pos: int, *, allow_roman: bool, is_first: bool
) -> tuple[Optional[str], list[str], int]:
    """吃掉一个「图号 + 可选子图标号」，返回 (归一化图号, 子图, 新位置)。"""
    m = _INT_ITEM_RE.match(text, pos)
    if m:
        # 续项才做单位检查：`Fig. 1` 后面的第一个数字必然是图号
        if not is_first and _looks_like_unit(text, m.end()):
            return None, [], pos
        num = norm_number(f"{m.group('pre') or ''}{m.group('digits')}")
        panels, end = _match_panel(text, m.end())
        return num, panels, end
    if allow_roman and is_first:
        m = _ROMAN_ITEM_RE.match(text, pos)
        if m and _roman_to_int(m.group("roman")) is not None:
            return norm_number(m.group("roman")), [], m.end()
    return None, [], pos


def _expand_range(prev: str, cur: str) -> list[str]:
    """`Figures 1-3` → 展开出中间的 2。非纯数字或跨度过大则不展开。"""
    if not (prev.isdigit() and cur.isdigit()):
        return []
    a, b = int(prev), int(cur)
    if 0 < b - a <= MAX_RANGE_SPAN:
        return [str(n) for n in range(a + 1, b)]
    return []


def find_figure_refs(text: str) -> list[FigureRef]:
    """扫描一段文本里所有的图/表引用。

    覆盖：``Fig. 1`` ``Fig 1`` ``Figure 1`` ``FIGURE 1`` ``figure 1`` ``Fig.1``、
    带子图的 ``Fig. 1A`` ``Figure 1(a)`` ``Fig. 1a-c``、
    复数与范围 ``Figs. 1 and 2`` ``Figures 1-3`` ``Fig. 1, 2``、
    表格 ``Table 2`` ``Tab. 3`` ``Table IV``、补充材料 ``Supplementary Fig. S1``、
    以及中文 ``图 1`` ``图1`` ``表 2`` ``图 1、2``。
    """
    if not text:
        return []
    refs: list[FigureRef] = []
    consumed_to = 0

    leads = sorted(
        list(_EN_LEAD_RE.finditer(text)) + list(_CN_LEAD_RE.finditer(text)),
        key=lambda m: m.start(),
    )
    for lead in leads:
        if lead.start() < consumed_to:  # 已被上一处引用吃掉（如 `Figs. 1 and 2` 内部）
            continue
        word = lead.group("word").lower()
        kind = "figure" if (word.startswith("fig") or word == "图") else "table"
        allow_roman = kind == "table"

        pos = lead.end()
        items: list[tuple[str, list[str]]] = []
        prev_sep_is_range = False
        while len(items) < 10:
            num, panels, new_pos = _match_item(
                text, pos, allow_roman=allow_roman, is_first=not items
            )
            if num is None:
                break
            if prev_sep_is_range and items:
                for mid in _expand_range(items[-1][0], num):
                    items.append((mid, []))
            items.append((num, panels))
            pos = new_pos

            for sep_re, is_range in (
                (_SEP_RANGE_RE, True),
                (_SEP_AND_RE, False),
                (_SEP_COMMA_RE, False),
            ):
                sm = sep_re.match(text, pos)
                if sm and sm.end() > sm.start():
                    # 分隔符后必须还跟着数字，否则不算续项（`Fig. 1 and Table 2`）
                    look, _, _ = _match_item(
                        text, sm.end(), allow_roman=False, is_first=False
                    )
                    if look is not None:
                        prev_sep_is_range = is_range
                        pos = sm.end()
                        break
            else:
                break

        if not items:
            continue
        raw = text[lead.start() : pos].strip()
        for num, panels in items:
            refs.append(
                FigureRef(
                    kind=kind,
                    number=num,
                    panels=panels,
                    raw=raw,
                    start=lead.start(),
                    end=pos,
                )
            )
        consumed_to = pos
    return refs


# ══════════════════════════════════════════════════════════════════════════
# 二、子图描述切分
# ══════════════════════════════════════════════════════════════════════════

#: 图题开头的标签，如 `Fig. 1.` `Figure 2:` `Table 3 |` `图 1．`
_CAP_LABEL_RE = re.compile(
    r"^[ \t]*(?:(?:supplementary|supplemental|extended[ \t]+data)[ \t]+)?"
    r"(?:figures?|figs?|tables?|tabs?|附?[图表])[ \t]*\.?[ \t]*"
    r"(?:[SE]?\d{1,3}|[IVXLC]{1,6})"
    r"(?:[ \t]*\((?:continued|cont\.?|续)\))?"
    r"[ \t]*[.:：、｜|—–-]?[ \t]*",
    re.IGNORECASE,
)

#: 子图标号候选。`(A)` / `(a-c)` / `A.` / `A)`。
#: **不要求前面有句读符号** —— 期刊图题经常直接接：
#: `... tumor growth (B) Measure the size ...`（真实 Fig. 5），要求句读会把 B 整个吞掉。
#: 裸标号前必须不是字母/数字/连字符，否则 `(cell nuclei)` 的 `i)`、`mean ± SD.` 的 `D.`、
#: `(FITC-H)` 的 `H)` 都会被当成标号。
_PANEL_MARKER_RE = re.compile(
    r"(?:"
    r"\((?P<par>[A-Za-z](?:[ \t]*(?:[-–—,、]|and|&)?[ \t]*[A-Za-z])*)\)"
    # Nature 系：小写字母 + 逗号，可合并多个（`a, The workflow …` / `a,b, Flowchart …`）。
    # 必须整体匹配 `a,b,`，拆成两次匹配会让 panel a 的描述里混进 `b,`。
    r"|(?<![A-Za-z0-9\-‑–—/])(?P<nat>[A-Za-z](?:[ \t]*,[ \t]*[A-Za-z])*),"
    r"|(?<![A-Za-z0-9\-‑–—/])(?P<bare>[A-Za-z])[.)]"
    r")(?=\s)"
)

#: 这些词后面的 `(B)` 是在**引用**子图而非标号：
#: `shown in (B)` / `intensity from (A)` / `Projection (a) shows ...`（真实 ACL Fig. 1）
_PANEL_REF_PREP_RE = re.compile(
    r"(?<![A-Za-z])(?:in|from|see|within|versus|vs|cf)[ \t]*$", re.IGNORECASE
)

#: 标号前紧挨着的限定名词：`Projection (a) shows ...`（真实 ACL Fig. 1）。
#: 它**是**标号，但描述要从这个名词起算——否则切出来是「shows the representations ...」
#: 这种没主语的残句，L3 读着费劲还容易误判实验内容。
#: 名词前面若还有介词（`in panel (B)`），那才是引用。
_PANEL_LEAD_NOUN_RE = re.compile(
    r"(?<![A-Za-z])(?P<prep>(?:in|from|see|within|of|for)[ \t]+)?"
    r"(?P<noun>projections?|panels?|plots?|subplots?|subfigures?|figures?|images?|"
    r"graphs?|charts?|diagrams?|curves?|columns?|rows?|parts?)[ \t]*$",
    re.IGNORECASE,
)

#: 段末悬着的连词要去掉：`... language-agnostic status, whereas` → `... language-agnostic status`
_SEG_TAIL_CONJ_RE = re.compile(
    r"[,;，；]?[ \t]*(?:whereas|whilst|while|however|and|but|or|then)[ \t]*$", re.IGNORECASE
)

#: 链头落在句首/句读之后只是**加分项**，两条链打平时才用它决胜，不作硬性要求。
#: 曾经把它当硬判据，结果 ACL Fig. 1 的 `Projection (a) shows ...` 被判成句中引用而漏切；
#: 但去看切图，那张图里实际画着 (a) (b) 两个虚线框子图，图题只是用散文在指它们。
#: **图题文法不能单独决定 panel 结构，图本身才是真相。**
#: L3 会读图，轻微过切它看图后能合并，漏切则连对应的上下文都没有 —— 边界上宁可倾向切分。
_PANEL_ANCHOR_RE = re.compile(r"(?:^|[.;:!?。；：！？\n])[ \t  ]*$")

#: 「全图共享收尾」的起始信号。这些句子写在最后一个 panel 描述之后，但适用于 A~N 全部子图：
#: `* indicates p < 0.05 ... N = 3 ... Data are expressed as mean ± SD.`（真实 Fig. 1~4 都这样）
#: 不切出来的话，A/B/C 三个 panel 的 n 与统计会被误报成 not_reported，而图题其实写了。
_TRAILER_RE = re.compile(
    r"(?:"
    r"\*+[ \t]*(?:indicates?|denotes?|means?|represents?|shows?|[Pp][ \t]*[<≤=])"
    r"|(?<![A-Za-z])[Nn][ \t]*=[ \t]*\d"
    r"|[Dd]ata[ \t]+(?:are|is|were|was)[ \t]+(?:expressed|presented|shown|given|reported)"
    r"|[Mm]ean[ \t]*±"
    r"|[Ee]rror[ \t]+bars?[ \t]+(?:denote|represent|indicate|show|are|correspond)"
    r"|[Ss]tatistical[ \t]+(?:analysis|significance|comparisons?)[ \t]+w(?:as|ere)"
    r"|[Ss]ignificance[ \t]+was"
    r"|(?:[Oo]ne|[Tt]wo)-way[ \t]+ANOVA"
    r"|[Ss]tudent'?s?[ \t]+t-test"
    r"|[Tt]he[ \t]+control[ \t]+group[ \t]+was[ \t]+used"
    r"|[Aa]ll[ \t]+(?:experiments?|assays?)[ \t]+were[ \t]+(?:performed|repeated|carried)"
    r"|数据(?:以|均以|表示为)"
    r"|与对照组(?:比较|相比)"
    r"|\*+[ \t]*表示"
    r"|平均值[ \t]*±"
    r")"
)
#: 收尾至少要落在末尾 panel 描述的这个字符数之后，避免把整个末尾 panel 判成收尾
MIN_TRAILER_OFFSET = 3


#: PDF 断词残留：`imply- ing` 是换行断词，`Protein- protein` 只是排版多了个空格。
#: 两者都要修，但修法相反 —— 前者去掉连字符，后者只去掉空格。
_HYPHEN_BREAK_RE = re.compile(r"(?<=[A-Za-z])-[ \t]+(?=[a-z])")
_WORD_SUFFIXES = frozenset(
    """ing ed s es tion tions sion sions ment ments ness ity ities ally able ible
    ive ous al ic ical er ers est ation ations ly ence ance ant ent ism ist ized
    ised izing ising ful less ship hood ward wise""".split()
)


def _dehyphenate(text: str) -> str:
    """修复 PDF 抽出的断词。

    ``imply- ing a language-agnostic status`` → ``implying a language-agnostic status``
    ``Protein- protein interaction``          → ``Protein-protein interaction``

    不修的话，被拆开的词既读着别扭，也会让 Methods 关键词召回漏命中。
    """
    if "-" not in text:
        return text

    def _join(m: re.Match) -> str:
        tail = re.match(r"[a-z]+", m.string[m.end() :])
        return "" if tail and tail.group(0) in _WORD_SUFFIXES else "-"

    return _HYPHEN_BREAK_RE.sub(_join, text)


def _desc_weight(text: str) -> int:
    """描述长度。中文按 2 计——「克隆形成实验」6 个字已是完整的 panel 描述。"""
    return sum(2 if ch >= "⺀" else 1 for ch in text)


def strip_caption_label(caption: str) -> str:
    """去掉图题开头的 `Fig. 1.` 之类标签，只留描述正文。"""
    return _CAP_LABEL_RE.sub("", caption or "", count=1).strip()


@dataclass
class _Marker:
    start: int  # 标号本身的起点
    end: int  # 标号本身的终点
    labels: list[str]
    anchored: bool  # 是否落在句首/句读之后（打平时的决胜项）
    cut: int  # 上一个 panel 的描述到此为止
    desc_start: int  # 本 panel 的描述从此开始（含被纳入的限定名词）
    lead_noun: bool = False  # 描述是否已含 `Projection` 这类限定名词
    #: 标号写法：`nat` = Nature 系 `a,` / `a,b,`；`par` = `(A)`；`bare` = `A.` / `A)`
    style: str = "par"


def _clean_segment(seg: str) -> str:
    """收拾一段 panel 描述：去首尾空白与悬着的逗号、去掉段末的连词。"""
    s = seg.strip().strip(";；,，").strip()
    s = _SEG_TAIL_CONJ_RE.sub("", s).strip()
    return s.strip(";；,，").strip()


def _split_trailer(segment: str) -> tuple[str, str]:
    """把末尾 panel 的描述与「全图共享收尾」切开。

    ``(D) Quantification of protein expression.* indicates p < 0.05 ... N = 3 ...``
    → ``("Quantification of protein expression.", "* indicates p < 0.05 ... N = 3 ...")``
    """
    m = _TRAILER_RE.search(segment)
    if m and m.start() >= MIN_TRAILER_OFFSET:
        return segment[: m.start()], segment[m.start() :].strip()
    return segment, ""


def split_panel_captions(caption: str) -> tuple[dict[str, str], str, str]:
    """把图题切成各 panel 的描述。

    返回 ``({"A": "...", "B": "..."}, 前置总述, 全图共享收尾)``。
    支持 ``(A) … (B) …``、``(a) … (b) …``、``A. … B. …``、``A) … B) …``，
    以及 ``(A-C) …``、``(A, B) …`` 这类一段描述覆盖多个 panel 的写法。

    真假标号的判据（全部来自真实图题的反例）：

    1. 标号必须从链头起**严格递增**（A→B→C）—— 主判据。断链的候选直接丢，
       这条挡住了 panel B 描述里出现的 ``intensity from (A)``
    2. ``shown in (B)`` / ``intensity from (A)`` 这种介词后的标号是纯引用，丢弃
    3. 链至少两节（从 A 起）；孤零零一个 ``(A)`` 判为句中括号，不切
    4. 标号后的描述要够长（中文字符按 2 计）

    标号**不要求**前面有句读符号：``... tumor growth (B) Measure the size ...`` 就没有。
    句读只在两条链打平时用来决胜。
    """
    body = _dehyphenate(strip_caption_label(caption))
    if not body:
        return {}, "", ""

    cands: list[_Marker] = []
    for m in _PANEL_MARKER_RE.finditer(body):
        if m.group("par") is not None:
            labels, style = _parse_panels(m.group("par")), "par"
        elif m.group("nat") is not None:
            labels, style = _parse_panels(m.group("nat")), "nat"
        else:
            labels, style = [m.group("bare").upper()], "bare"
        if not labels:
            continue
        prefix = body[: m.start()]
        if _PANEL_REF_PREP_RE.search(prefix):  # 判据 2
            continue
        cut = m.start()
        desc_start = m.end()
        lead_noun = False
        nm = _PANEL_LEAD_NOUN_RE.search(prefix)
        if nm is not None:
            if nm.group("prep"):  # `in panel (B)` 仍是引用
                continue
            cut = desc_start = nm.start("noun")  # `Projection (a) shows ...` 整句归本 panel
            lead_noun = True
        cands.append(
            _Marker(
                m.start(), m.end(), labels,
                bool(_PANEL_ANCHOR_RE.search(prefix)), cut, desc_start, lead_noun,
                style,
            )
        )
    if len(cands) < 2:
        return {}, "", ""

    # 判据 1：从每个候选出发取最长严格递增链；打平时优先「从 A 起」再优先「落在句读后」
    best: list[_Marker] = []
    best_key = (-1, -1, -1)
    for i, head in enumerate(cands):
        chain = [head]
        expect = ord(head.labels[-1]) + 1
        for nxt in cands[i + 1 :]:
            if ord(nxt.labels[0]) == expect:
                chain.append(nxt)
                expect = ord(nxt.labels[-1]) + 1
        key = (
            1 if chain[0].labels[0] == "A" else 0,
            # 一条图题内标号写法应当一致。Nature 系用 `a,` 起头时，描述里的
            # `Flowchart (a) and characteristics (b)` 是**指认**而非新 panel——
            # 若只按链长取胜，那两个括号会赢，把 `a,b,` 共享的描述切成两截残句。
            # 故风格优先级高于链长。
            1 if chain[0].style == "nat" else 0,
            len(chain),
            1 if head.anchored else 0,
        )
        if key > best_key:
            best_key, best = key, chain

    starts_at_a, _is_nat, n_links, _ = best_key
    if not best or n_links < 2 or not (starts_at_a or n_links >= 3):  # 判据 3
        return {}, "", ""

    panels: dict[str, str] = {}
    trailer = ""
    for idx, mk in enumerate(best):
        stop = best[idx + 1].cut if idx + 1 < len(best) else len(body)
        seg = body[mk.desc_start : stop]
        if idx == len(best) - 1:  # 只有最后一段才可能带全图共享收尾
            seg, trailer = _split_trailer(seg)
        desc = _clean_segment(seg)
        # 描述以小写词开头说明它是承接标号的谓语（`(a) shows the results ...`），
        # 把标号补回去，别留一个没主语的残句给 L3
        if desc[:1].islower() and not mk.lead_noun:
            desc = f"{body[mk.start : mk.end].strip()} {desc}"
        if _desc_weight(desc) < MIN_PANEL_DESC_CHARS:  # 判据 4
            continue
        for lab in mk.labels:
            panels[lab] = desc
    if not panels:
        return {}, "", ""

    preamble = body[: best[0].cut].strip().strip(".;:：。；").strip()
    return panels, preamble, trailer


# ══════════════════════════════════════════════════════════════════════════
# 三、方法学关键词抽取与 Methods 召回
# ══════════════════════════════════════════════════════════════════════════

# 打分只用「关键词重合数 × 类别权重 × 来源权重 + 章节加成」，
# 不引入 embedding：召回结果要能在 md 里逐条解释「为什么选中这段」。

_CATEGORY_WEIGHTS = {
    "cell_line": 1.6,
    "model": 1.6,
    "dataset": 1.6,
    "animal": 1.5,
    "assay": 1.4,
    "reagent": 1.4,
    "code": 1.3,  # 通用「字母+数字」实体：U251 / GPT-4 / IC50 / CCK-8
    "metric": 1.1,
    "acronym": 1.0,  # 通用大写缩写：GSH / ROS / IHC / BLEU
    "hyperparam": 0.8,
    "generic": 0.6,  # antibody / kit / 剂量单位这类弱信号
}

_GAZETTEERS: dict[str, tuple[str, ...]] = {
    "cell_line": (
        "U251", "U-251", "U87", "U-87", "U373", "A172", "LN229", "LN-229", "T98G",
        "HeLa", "A549", "MCF-7", "MCF7", "MDA-MB-231", "HEK293", "HEK293T", "293T",
        "HCT116", "HT-29", "SW480", "SH-SY5Y", "PC-3", "PC3", "HepG2", "Huh7",
        "Jurkat", "THP-1", "RAW264.7", "BV2", "4T1", "B16", "NIH3T3", "Vero", "GL261",
    ),
    "animal": (
        "BALB/c", "C57BL/6", "nude mice", "athymic", "Sprague-Dawley", "SD rats",
        "Wistar", "NOD/SCID", "NSG mice", "transgenic mice", "xenograft model",
        "orthotopic", "裸鼠", "小鼠", "大鼠",
    ),
    "assay": (
        "CCK-8", "CCK8", "MTT assay", "western blot", "western blotting", "immunoblot",
        "qPCR", "RT-qPCR", "qRT-PCR", "real-time PCR", "RT-PCR", "flow cytometry",
        "FACS", "immunohistochemistry", "IHC", "immunofluorescence", "ELISA", "TUNEL",
        "colony formation", "clonogenic", "wound healing", "scratch assay", "transwell",
        "migration assay", "invasion assay", "EdU", "BrdU", "co-immunoprecipitation",
        "co-IP", "ChIP", "luciferase reporter", "dual-luciferase", "transfection",
        "siRNA", "shRNA", "sgRNA", "CRISPR", "lentivirus", "plasmid", "xenograft",
        "subcutaneous", "intraperitoneal", "H&E", "hematoxylin", "Annexin V",
        "propidium iodide", "JC-1", "DCFH-DA", "transmission electron microscopy",
        "cell viability", "apoptosis", "ferroptosis", "autophagy", "cell cycle",
        "knockdown", "overexpression", "RNA-seq", "transcriptome", "GO enrichment",
        "KEGG", "Kaplan-Meier", "survival analysis", "immunoprecipitation",
        "细胞活力", "凋亡", "免疫组化", "流式细胞", "蛋白免疫印迹",
    ),
    "reagent": (
        "temozolomide", "TMZ", "erastin", "ferrostatin-1", "Fer-1", "RSL3",
        "liproxstatin", "DMSO", "DMEM", "FBS", "trypsin", "paraformaldehyde",
        "RIPA", "z-VAD", "N-acetylcysteine", "cisplatin", "doxorubicin", "paclitaxel",
        "curcumin", "rapamycin", "chloroquine", "MG132", "deferoxamine", "PBS",
    ),
    "model": (
        "LLaMA", "Llama", "Llama-2", "Llama-3", "BLOOM", "BLOOMZ", "XGLM", "GPT-2",
        "GPT-3", "GPT-3.5", "GPT-4", "GPT-4o", "ChatGPT", "mT5", "mT0", "flan-T5",
        "T5", "mBERT", "BERT", "RoBERTa", "XLM-R", "XLM-RoBERTa", "Mistral", "Mixtral",
        "Qwen", "Falcon", "OPT", "Gemma", "Baichuan", "ChatGLM", "NLLB", "M2M-100",
        "Whisper", "ViT", "ResNet", "Transformer", "LSTM", "Pythia", "StarCoder",
        "CodeLlama", "PaLM", "Gemini", "Claude",
    ),
    "dataset": (
        "FLORES", "FLORES-200", "XNLI", "MGSM", "MMLU", "XQuAD", "TyDiQA", "WMT",
        "GLUE", "SuperGLUE", "SQuAD", "CommonCrawl", "OSCAR", "XCOPA", "PAWS-X",
        "BELEBELE", "HellaSwag", "GSM8K", "HumanEval", "MBPP", "Alpaca", "ShareGPT",
        "WikiText", "ImageNet", "CIFAR", "MS MARCO", "BIG-bench", "AGIEval",
        "C-Eval", "CMMLU", "TruthfulQA", "WinoGrande", "XL-Sum", "MLQA",
    ),
    "metric": (
        "BLEU", "spBLEU", "sacreBLEU", "chrF", "chrF++", "COMET", "METEOR", "ROUGE",
        "ROUGE-L", "accuracy", "F1", "macro-F1", "micro-F1", "precision", "recall",
        "perplexity", "exact match", "AUC", "AUROC", "MRR", "nDCG", "pass@1",
        "win rate", "IC50",
    ),
    "hyperparam": (
        "learning rate", "batch size", "epochs", "optimizer", "AdamW", "warmup",
        "weight decay", "dropout", "beam size", "beam search", "greedy decoding",
        "temperature", "top-p", "top-k", "nucleus sampling", "fine-tuning",
        "fine-tuned", "LoRA", "QLoRA", "PEFT", "zero-shot", "few-shot",
        "in-context learning", "chain-of-thought", "instruction tuning",
        "pre-training", "checkpoint", "random seed", "tokenizer", "context length",
        "gradient accumulation", "A100", "V100", "H100",
    ),
    "generic": (
        "antibody", "antibodies", "kit", "reagent", "primer", "medium", "incubated",
        "seeded", "prompt", "baseline", "ablation", "statistical analysis",
        "one-way ANOVA", "ANOVA", "t-test", "GraphPad", "SPSS", "p < 0.05",
    ),
}

#: 「字母+数字」实体：U251 / GPT-4 / CCK-8 / IC50 / HMOX1 / SLC7A11
_CODE_RE = re.compile(r"\b(?P<alpha>[A-Za-z]{1,6})[-‑]?(?P<digit>\d{1,4})(?P<tail>[A-Za-z]?\d{0,3})\b")
#: `p53` / `n` 这类要留（基因名），但 `Fig1` / `Eq3` / `24h` 是排版噪音
_CODE_STOP = {
    "fig", "figs", "figure", "table", "tab", "eq", "eqn", "eqs", "ref", "refs", "no",
    "s", "ch", "sec", "vol", "pp", "et", "al", "h", "d", "day", "days", "hr",
    "min", "mg", "kg", "ml", "ug", "ng", "nm", "mm", "cm", "um", "top", "line",
    "app", "supp", "page", "step", "row", "col",
}
#: 通用大写缩写：GSH / ROS / IHC / BLEU / XNLI / TMZ。
#: 带连字符的必须整体捕获——`DMC-GF` 拆成 `DMC` + `GF` 会把药名这个最关键的实体拆没
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,8}(?:[-‑][A-Z0-9]{1,8})*\b")
_ACRONYM_STOP = {
    "THE", "AND", "FOR", "NOT", "ALL", "ANY", "ONE", "TWO", "BUT", "WITH", "FROM",
    "THAT", "THIS", "THESE", "THOSE", "WERE", "WAS", "ARE", "HAVE", "HAS", "FIG",
    "FIGS", "TAB", "TABLE", "SD", "SEM", "CI", "NS", "VS", "ET", "AL", "DNA", "RNA",
    "USA", "PDF", "HTML", "JSON", "API", "CPU", "II", "III", "IV", "VI", "VII", "IX",
    "OR", "IN", "OF", "TO", "AS", "AT", "BY", "ON", "IS", "IT", "WE", "OUR", "ITS",
}
#: 剂量/时长这类弱但有用的信号
_DOSE_RE = re.compile(
    r"\b\d+(?:\.\d+)?[ \t]*(?:μM|µM|uM|mM|nM|mg/kg|μg/mL|µg/mL|mg/mL|ng/mL|IU|Gy|%)",
    re.IGNORECASE,
)


@dataclass
class Keyword:
    """一个用于 Methods 召回的方法学关键词。"""

    text: str  # 展示用原文
    key: str  # 归一化去重键（小写）
    category: str
    origin: str  # "caption" | "body"
    regex: re.Pattern = field(repr=False, default=None)  # type: ignore[assignment]

    @property
    def origin_weight(self) -> float:
        # 图题里的词最能代表这张图做了什么，权重高于正文引用段
        return 2.0 if self.origin == "caption" else 1.0

    @property
    def weight(self) -> float:
        return _CATEGORY_WEIGHTS.get(self.category, 1.0) * self.origin_weight


@lru_cache(maxsize=4096)
def _kw_regex(text: str) -> re.Pattern:
    """带边界的关键词匹配。

    三条规则都是被真实图题逼出来的：

    - 中文无 ``\\b`` 概念，直接子串匹配。
    - **允许常见词形变化**：图题写 ``Western blot``、Methods 章节标题写
      ``Western blotting``，不容忍词尾变化就永远召回不到那一段。
      末尾辅音可叠写（blot → blot**t**ing），故后缀式为 ``t?(?:e?s|e?d|ing)``。
    - 短的全大写缩写（ROS / GSH / F1）**区分大小写且不加后缀**，
      否则 ``ROS`` 会命中 ``roses``。
    """
    if re.search(r"[一-鿿]", text):
        return re.compile(re.escape(text))
    flags = 0 if (len(text) <= 4 and text.isupper()) else re.IGNORECASE
    suffix = ""
    if len(text) >= 5 or " " in text:
        last = text[-1]
        dbl = f"{re.escape(last)}?" if last.isalpha() and last.lower() not in "aeiou" else ""
        suffix = rf"(?:{dbl}(?:e?s|e?d|ing))?"
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(text)}{suffix}(?![A-Za-z0-9])", flags)


def extract_keywords(caption_text: str, body_text: str) -> list[Keyword]:
    """从图题（高权重）与引用段（低权重）里抽方法学关键词。

    类别覆盖生物医学（细胞系 / 动物品系 / 试剂 / assay / 剂量单位）与
    ML/NLP（模型名 / 数据集 / 指标 / 超参），外加两条通用兜底模式
    （字母+数字实体、大写缩写），后者负责捞基因名与本表未收录的新模型名。
    """
    found: dict[str, Keyword] = {}

    def add(text: str, category: str, origin: str) -> None:
        text = text.strip()
        if len(text) < 2:
            return
        key = text.lower().replace("‑", "-")
        old = found.get(key)
        cand = Keyword(text=text, key=key, category=category, origin=origin,
                       regex=_kw_regex(text))
        if old is None:
            found[key] = cand
            return
        # 同一个词以「更高权重」的那次为准（图题优先、具体类别优先）
        if cand.weight > old.weight:
            found[key] = cand

    for origin, text in (("caption", caption_text or ""), ("body", body_text or "")):
        if not text:
            continue
        for category, terms in _GAZETTEERS.items():
            for term in terms:
                if _kw_regex(term).search(text):
                    add(term, category, origin)
        for m in _CODE_RE.finditer(text):
            token = m.group(0)
            if m.group("alpha").lower() in _CODE_STOP or len(token) < 3:
                continue
            add(token, "code", origin)
        for m in _ACRONYM_RE.finditer(text):
            if m.group(0) in _ACRONYM_STOP:
                continue
            add(m.group(0), "acronym", origin)
        for m in _DOSE_RE.finditer(text):
            add(re.sub(r"[ \t]+", " ", m.group(0)), "generic", origin)

    kws = sorted(found.values(), key=lambda k: (-k.weight, k.key))
    return kws[:MAX_KEYWORDS]


#: 统计方法章节。图题几乎从不写检验方法，靠关键词永远召不回来，但 schema 的
#: `stats.test` / `error_bar` / `significance` 每张图都要填 —— 所以固定附上。
_STATS_TITLE_RE = re.compile(r"statistic|统计|significance test", re.IGNORECASE)
MAX_PINNED_STATS = 2


@dataclass
class MethodsHit:
    """一段 Methods 的召回结果，带可解释的打分明细。"""

    paragraph: Paragraph
    score: float
    matched: list[str]
    section_bonus: float = 0.0
    fallback: bool = False  # 关键词零命中时的兜底
    pinned: bool = False  # 统计方法段，不打分直接附上


def _section_bonus(section: Optional[Section], caption_keywords: Iterable[Keyword]) -> float:
    """章节优先级加成：标题像 Methods 的 +0.5，标题里出现图题关键词的每个 +1.0。"""
    if section is None:
        return 0.0
    title = section.title.lower()
    bonus = 0.0
    if any(
        t in title
        for t in ("method", "experimental setup", "experimental settings", "materials",
                  "实验方法", "材料与方法", "实验设置")
    ):
        bonus += 0.5
    for kw in caption_keywords:
        if kw.regex.search(section.title):
            bonus += 1.0
    return bonus


def _length_penalty(text: str) -> float:
    """温和的长度惩罚：避免「通用试剂清单」这类超长段仅靠体量霸榜。"""
    n_words = max(1, len(text.split()))
    if n_words <= 250:
        return 1.0
    return (250.0 / n_words) ** 0.5


def recall_methods(
    keywords: list[Keyword],
    methods_paragraphs: list[Paragraph],
    sections_by_id: dict[str, Section],
    *,
    top_k: int = METHODS_TOP_K,
    exclude_ids: Optional[set[str]] = None,
) -> tuple[list[MethodsHit], list[MethodsHit]]:
    """关键词召回 Methods 段落，返回 (入选, 落选但有命中)。

    单段得分 = Σ(命中关键词的 类别权重 × 来源权重) × 长度惩罚 + 章节加成。
    每个关键词在一段里只计一次，避免同一个词刷分。
    """
    exclude_ids = exclude_ids or set()
    caption_kws = [k for k in keywords if k.origin == "caption"]
    scored: list[MethodsHit] = []
    for para in methods_paragraphs:
        if para.id in exclude_ids:
            continue
        matched: list[str] = []
        raw = 0.0
        for kw in keywords:
            if kw.regex.search(para.text):
                raw += kw.weight
                matched.append(kw.text)
        if not matched:
            continue
        bonus = _section_bonus(sections_by_id.get(para.section_id), caption_kws)
        score = raw * _length_penalty(para.text) + bonus
        scored.append(MethodsHit(paragraph=para, score=round(score, 2),
                                 matched=matched, section_bonus=bonus))

    scored.sort(key=lambda h: (-h.score, h.paragraph.id))
    picked = [h for h in scored if h.score >= MIN_METHODS_SCORE][:top_k]

    if not picked and methods_paragraphs:
        # 一个关键词都没命中（常见于示意图）。给两段兜底总比留空强，md 里会标注来源。
        for para in methods_paragraphs[:METHODS_FALLBACK_N]:
            if para.id not in exclude_ids:
                picked.append(MethodsHit(paragraph=para, score=0.0, matched=[], fallback=True))

    # 统计方法段固定附上（不占 top_k 名额）
    picked_ids = {h.paragraph.id for h in picked}
    n_pinned = 0
    for para in methods_paragraphs:
        if n_pinned >= MAX_PINNED_STATS:
            break
        if para.id in picked_ids or para.id in exclude_ids:
            continue
        sec = sections_by_id.get(para.section_id)
        if sec is not None and _STATS_TITLE_RE.search(sec.title):
            picked.append(MethodsHit(paragraph=para, score=0.0, matched=[], pinned=True))
            picked_ids.add(para.id)
            n_pinned += 1

    picked_ids = {h.paragraph.id for h in picked}
    rejected = [h for h in scored if h.paragraph.id not in picked_ids]
    return picked, rejected


# ══════════════════════════════════════════════════════════════════════════
# 四、装配
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class PackDiagnostics:
    """装配过程的可解释记录，只进 md 不进 JSON（ContextPack 契约不含它）。"""

    match_key: tuple[str, str] = ("figure", "")
    keywords: list[Keyword] = field(default_factory=list)
    mentions: dict[str, list[str]] = field(default_factory=dict)  # para.id → 原文引用片段
    panels_by_para: dict[str, list[str]] = field(default_factory=dict)
    methods_hits: list[MethodsHit] = field(default_factory=list)
    methods_rejected: list[MethodsHit] = field(default_factory=list)
    dropped_citing: list[str] = field(default_factory=list)
    caption_lead: str = ""
    warnings: list[str] = field(default_factory=list)


#: 序列名 → 文件名前缀。必须与 `figures._SERIES_DISPLAY` 保持一致，
#: 否则 context 的 md 文件名会和 L1 切出的 png 文件名对不上。
_SERIES_STEM = {"": "", "extended data": "ed", "supplementary": "s", "appendix": "ap"}


def asset_stem(asset: FigureAsset) -> str:
    """`Figure 1` → `fig_01`，`Table 3` → `tab_03`，`Extended Data Figure 1` → `edfig_01`。

    figure_id 含空格与点号，不能直接当文件名；这里统一规范化并零填充到两位，
    与 L1 切图 `figures/fig_01.png` 的命名保持一致。

    **序列前缀不可省**——正文 Fig.1 与 Extended Data Fig.1 同号，
    漏掉前缀两者会抢同一个文件名。
    """
    prefix = "tab" if asset.kind == "table" else "fig"
    prefix = _SERIES_STEM.get(asset.series, asset.series.replace(" ", "")) + prefix
    raw = (asset.number or "").strip()
    m = re.fullmatch(r"(?i)([se]d?)?[ \t]?0*(\d+)([a-z]?)", raw)
    if m:
        return f"{prefix}_{(m.group(1) or '').lower()}{int(m.group(2)):02d}{(m.group(3) or '').lower()}"
    roman = _roman_to_int(raw)
    if roman is not None:
        return f"{prefix}_{roman:02d}"
    slug = re.sub(r"[^0-9A-Za-z]+", "_", raw or asset.figure_id).strip("_").lower()
    return f"{prefix}_{slug or 'x'}"


def _sections_by_id(parsed: ParsedPaper) -> dict[str, Section]:
    return {s.id: s for s in parsed.sections}


def _section_breadcrumb(sections_by_id: dict[str, Section], section_id: str) -> str:
    """`§3.2` → `§3 Results › §3.2 Ablation study`。"""
    sid = (section_id or "").strip()
    if not sid:
        return ""
    parts = sid.lstrip("§").split(".")
    chain = []
    for i in range(1, len(parts) + 1):
        sec = sections_by_id.get("§" + ".".join(parts[:i]))
        if sec is not None:
            chain.append(f"{sec.id} {sec.title}")
    return " › ".join(chain) if chain else sid


def _is_caption_echo(para: Paragraph, caption: str) -> bool:
    """段落其实就是图题本身（部分 PDF 会把图题也抽成正文段），去重掉。"""
    if not caption:
        return False
    a = re.sub(r"\s+", " ", para.text).strip()
    b = re.sub(r"\s+", " ", caption).strip()
    if not a or not b:
        return False
    head = b[:60]
    return a.startswith(head) or (len(a) < len(b) * 1.2 and b.startswith(a[:60]))


def _build_one(
    asset: FigureAsset,
    paragraphs: list[Paragraph],
    sections_by_id: dict[str, Section],
    ref_index: dict[str, list[FigureRef]],
    methods_paragraphs: list[Paragraph],
) -> tuple[ContextPack, PackDiagnostics]:
    diag = PackDiagnostics(match_key=(asset.kind, norm_number(asset.number)))
    key = diag.match_key
    caption = _dehyphenate(asset.caption)

    # 1) 正文交叉引用
    citing: list[Paragraph] = []
    for para in paragraphs:
        hits = [r for r in ref_index.get(para.id, ()) if r.key == key]
        if not hits:
            continue
        if _is_caption_echo(para, caption):
            continue
        citing.append(para)
        diag.mentions[para.id] = sorted({r.raw for r in hits})
        panels = sorted({p for r in hits for p in r.panels})
        if panels:
            diag.panels_by_para[para.id] = panels

    if len(citing) > MAX_CITING_PARAGRAPHS:
        diag.dropped_citing = [p.id for p in citing[MAX_CITING_PARAGRAPHS:]]
        citing = citing[:MAX_CITING_PARAGRAPHS]
        diag.warnings.append(
            f"引用段过多，仅保留文档序前 {MAX_CITING_PARAGRAPHS} 段"
            f"（略过 {len(diag.dropped_citing)} 段，见诊断）"
        )
    if not citing:
        diag.warnings.append("正文中未找到引用本图的段落 —— 抽取只能靠图题 + 读图，请人工确认")

    # 2) 子图切分
    panel_captions, preamble, trailer = split_panel_captions(caption)
    diag.caption_lead = preamble
    if not panel_captions and asset.kind == "figure":
        diag.warnings.append("图题里未切出子图标号（可能本就是单 panel 图，或图题排版特殊）")

    # 3) Methods 召回
    body_text = "\n".join(p.text for p in citing)
    caption_text = "\n".join([caption, *panel_captions.values()])
    keywords = extract_keywords(caption_text, body_text)
    diag.keywords = keywords
    picked, rejected = recall_methods(
        keywords, methods_paragraphs, sections_by_id,
        exclude_ids={p.id for p in citing},
    )
    diag.methods_hits = picked
    diag.methods_rejected = rejected[:5]
    if not methods_paragraphs:
        diag.warnings.append("全文没有 is_methods 章节，无法召回方法学细节")
    elif all(h.fallback for h in picked) and picked:
        diag.warnings.append("关键词零命中，Methods 段落按章节顺序兜底选取")

    pack = ContextPack(
        figure_id=asset.figure_id,
        kind=asset.kind,
        caption=caption,
        images=list(asset.images),
        pages=list(asset.pages),
        citing_paragraphs=citing,
        methods_paragraphs=[h.paragraph for h in picked],
        panel_captions=panel_captions,
        caption_preamble=preamble,
        caption_trailer=trailer,
        table_text_grid=asset.table_text_grid,
        bbox_confidence=asset.bbox_confidence,
    )
    return pack, diag


def build_context_packs(parsed: ParsedPaper, out_dir: Path) -> list[ContextPack]:
    """L2 入口：为 `parsed` 里的每个 asset 装配上下文，并写出人可读的 md。

    单图失败不中断整体：退化成「只有图题」的 pack，warning 打到 stderr 并写进 md。
    """
    out_dir = Path(out_dir)
    ctx_dir = out_dir / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)

    sections_by_id = _sections_by_id(parsed)
    # 先统一修断词。ACL 这类 LaTeX 排版的正文里 `imply- ing` 很多，
    # 不修会同时伤到可读性和 Methods 关键词召回。段落 id 不变，溯源不受影响。
    paragraphs = [
        p if "-" not in p.text else p.model_copy(update={"text": _dehyphenate(p.text)})
        for p in parsed.paragraphs
    ]
    methods_paragraphs = [
        p for p in paragraphs
        if (sec := sections_by_id.get(p.section_id)) is not None and sec.is_methods
    ]

    # 每段只扫一次引用，所有图共用这份索引
    ref_index: dict[str, list[FigureRef]] = {}
    for para in paragraphs:
        try:
            ref_index[para.id] = find_figure_refs(para.text)
        except Exception as exc:  # pragma: no cover — 正则兜底，不该发生
            ref_index[para.id] = []
            print(f"[L2][warn] 段落 {para.id} 引用扫描失败：{exc}", file=sys.stderr)

    packs: list[ContextPack] = []
    used_stems: set[str] = set()
    for asset in parsed.assets:
        try:
            pack, diag = _build_one(
                asset, paragraphs, sections_by_id, ref_index, methods_paragraphs
            )
        except Exception as exc:
            diag = PackDiagnostics(
                match_key=(asset.kind, norm_number(asset.number)),
                warnings=[f"装配失败，已退化为「仅图题」上下文：{type(exc).__name__}: {exc}"],
            )
            pack = ContextPack(
                figure_id=asset.figure_id,
                kind=asset.kind,
                caption=asset.caption,
                images=list(asset.images),
                pages=list(asset.pages),
                table_text_grid=asset.table_text_grid,
                bbox_confidence=asset.bbox_confidence,
            )
        packs.append(pack)

        stem = asset_stem(asset)
        if stem in used_stems:  # 图号重复时不互相覆盖
            n = 2
            while f"{stem}_{n}" in used_stems:
                n += 1
            stem = f"{stem}_{n}"
            diag.warnings.append(f"figure_id 重复，本图 md 落到 {stem}.md")
        used_stems.add(stem)

        for w in diag.warnings:
            print(f"[L2][warn] {asset.figure_id}: {w}", file=sys.stderr)
        try:
            md = render_context_md(pack, diag, asset, sections_by_id, parsed)
            with open(ctx_dir / f"{stem}.md", "w", encoding="utf-8", newline="\n") as fh:
                fh.write(md)
        except Exception as exc:  # 渲染失败不能拖累 pack 本身
            print(f"[L2][warn] {asset.figure_id}: context md 渲染失败：{exc}", file=sys.stderr)

    return packs


# ══════════════════════════════════════════════════════════════════════════
# 五、渲染人可读 context md
# ══════════════════════════════════════════════════════════════════════════

_HOWTO = """> **怎么用这份文件**：前面几节是抽取这张图所需的全部原文，最后一节是装配诊断。
> - `§x.y¶n` 是段落 id，填 `evidence.from_body` / `evidence.from_methods` 时原样抄。
> - 图题里写明的信息填 `evidence.from_caption`；只能从图里读出的填 `from_image_only`。
> - **原文没写就填 `not_reported`**，尤其是 `n`、剂量、统计检验三项，不许从常识补。"""


def _md_verbatim(text: str) -> str:
    """原文照登，但要挡住 markdown 语法。

    图题结尾几乎必有 ``* indicates p < 0.05, ** indicates p < 0.01`` ——
    不转义的话渲染成项目符号 + 加粗，显著性定义直接看不见了。
    """
    out = (text or "").replace("*", "\\*")
    return re.sub(r"^([ \t]*)([#>+|-])", r"\1\\\2", out, flags=re.MULTILINE)


def _md_escape_cell(text: str) -> str:
    cell = (text or "").replace("|", "\\|").replace("*", "\\*")
    return re.sub(r"\s+", " ", cell).strip()


def _fmt_kw(kws: list[Keyword]) -> str:
    return "、".join(f"`{k.text}`({k.category})" for k in kws) or "（无）"


def render_context_md(
    pack: ContextPack,
    diag: PackDiagnostics,
    asset: FigureAsset,
    sections_by_id: dict[str, Section],
    parsed: ParsedPaper,
) -> str:
    """把一个 pack 渲染成人可读的 markdown。"""
    L: list[str] = []
    a = L.append

    a(f"# {pack.figure_id} · 上下文包")
    a("")
    a("| 项 | 值 |")
    a("|---|---|")
    a(f"| 类型 | {pack.kind} |")
    a(f"| 页码 | {', '.join(f'p{p}' for p in pack.pages) or '未知'} |")
    a(f"| 切图 | {', '.join(f'`{i}`' for i in pack.images) or '（无）'} |")
    a(f"| 切图置信度 | {pack.bbox_confidence.value} |")
    a(f"| 正文引用段 | {len(pack.citing_paragraphs)} |")
    a(f"| Methods 召回段 | {len(pack.methods_paragraphs)} |")
    a(f"| 子图数 | {len(pack.panel_captions)} |")
    if parsed.title:
        a(f"| 论文 | {_md_escape_cell(parsed.title)} |")
    a("")
    a(_HOWTO)

    if diag.warnings or asset.warnings:
        a("")
        a("> [!warning] 装配告警")
        for w in list(asset.warnings) + diag.warnings:
            a(f"> - {w}")
    a("")
    a("---")
    a("")

    # 1 图题
    a("## 1 · 图题原文")
    a("")
    a(_md_verbatim(pack.caption.strip()) or "_（L1 未抽到图题）_")
    a("")
    if pack.panel_captions:
        # 前置总述与收尾都**适用于全图每一个 panel**，必须与子图描述分开呈现，
        # 否则 `N = 3` / `mean ± SD` / `*` 的含义会被误当成末尾 panel 专属，
        # 前面几个 panel 白白填成 not_reported。
        if pack.caption_preamble:
            a("### 图题前置总述 · 适用于全图各 panel")
            a("")
            a(_md_verbatim(pack.caption_preamble))
            a("")
        a("### 子图描述（从图题切出）")
        a("")
        a("| 标号 | 描述 |")
        a("|---|---|")
        for lab in sorted(pack.panel_captions):
            a(f"| **{lab}** | {_md_escape_cell(pack.panel_captions[lab])} |")
        a("")
        if pack.caption_trailer:
            a("### 图题收尾说明 · **适用于全图各 panel**")
            a("")
            a(_md_verbatim(pack.caption_trailer))
            a("")
            a("> 上面这段写在最后一个子图描述之后，但**管着 A~"
              f"{max(pack.panel_captions)} 每一个 panel**。"
              "`n` / `error_bar` / `significance` / `test` 从这里填，"
              "不要因为它排在末尾就只算给最后一个 panel，也不要填 `not_reported`。")
            a("")
    else:
        a("_图题里没有切出子图标号：整图按单个 panel 处理（`label: \"-\"`），"
          "或子图标号只画在图上、需读图确认。_")
        a("")

    # 2 正文引用段
    a("---")
    a("")
    a(f"## 2 · 正文引用段（{len(pack.citing_paragraphs)} 段）")
    a("")
    if not pack.citing_paragraphs:
        a("_正文里没有检索到引用本图的段落。抽取只能依赖图题与读图，`confidence` 建议降级。_")
        a("")
    for para in pack.citing_paragraphs:
        crumb = _section_breadcrumb(sections_by_id, para.section_id)
        mentions = "、".join(f"`{m}`" for m in diag.mentions.get(para.id, []))
        panels = diag.panels_by_para.get(para.id)
        meta = [f"p{para.page}"]
        if crumb:
            meta.insert(0, crumb)
        if mentions:
            meta.append(f"提及 {mentions}")
        if panels:
            meta.append(f"涉及子图 {', '.join(panels)}")
        a(f"### `{para.id}` · {' · '.join(meta)}")
        a("")
        a(_md_verbatim(para.text.strip()))
        a("")

    # 3 Methods 召回
    a("---")
    a("")
    a(f"## 3 · Methods 召回段（{len(pack.methods_paragraphs)} 段）")
    a("")
    if not pack.methods_paragraphs:
        a("_没有召回到 Methods 段落：剂量 / n / 统计方法很可能无从查证，一律 `not_reported`。_")
        a("")
    for hit in diag.methods_hits:
        para = hit.paragraph
        crumb = _section_breadcrumb(sections_by_id, para.section_id)
        if hit.pinned:
            why = "统计方法段，固定附上（`stats.test` / `error_bar` / `significance` 从这里填）"
        elif hit.fallback:
            why = "兜底选取（关键词零命中）"
        else:
            why = f"score {hit.score} · 命中 " + "、".join(f"`{m}`" for m in hit.matched[:10])
            if len(hit.matched) > 10:
                why += f" 等 {len(hit.matched)} 个"
        a(f"### `{para.id}` · {crumb or para.section_id} · p{para.page}")
        a("")
        a(f"<sub>召回依据：{why}</sub>")
        a("")
        a(_md_verbatim(para.text.strip()))
        a("")

    # 4 表格文本网格（只有表格才有，之后的小节号顺延）
    sec_no = 4
    if pack.kind == "table" or pack.table_text_grid:
        a("---")
        a("")
        a(f"## {sec_no} · 表格文本网格（用于数字交叉校验）")
        sec_no += 1
        a("")
        grid = pack.table_text_grid or []
        if not grid:
            a("_L1 未还原出文本网格，请直接读切图。_")
            a("")
        else:
            width = max(len(r) for r in grid)
            header = [_md_escape_cell(c) for c in grid[0]] + [""] * (width - len(grid[0]))
            a("| " + " | ".join(header) + " |")
            a("|" + "---|" * width)
            for row in grid[1:]:
                cells = [_md_escape_cell(c) for c in row] + [""] * (width - len(row))
                a("| " + " | ".join(cells) + " |")
            a("")
            a("_以上为文本层还原，可能有串列；与切图不一致时以切图为准。_")
            a("")

    # 诊断
    a("---")
    a("")
    a(f"## {sec_no} · 装配诊断（调试用，抽取时可忽略）")
    a("")
    a(f"- 图号匹配键：`{diag.match_key[0]}/{diag.match_key[1]}`")
    a(f"- 方法学关键词（{len(diag.keywords)} 个，用于 Methods 召回）")
    cap_kw = [k for k in diag.keywords if k.origin == "caption"]
    body_kw = [k for k in diag.keywords if k.origin == "body"]
    a(f"  - 出现在图题（权重 ×2）：{_fmt_kw(cap_kw)}")
    a(f"  - 仅出现在引用段（权重 ×1）：{_fmt_kw(body_kw)}")
    if diag.methods_rejected:
        a("- 有命中但未入选的 Methods 段：")
        for hit in diag.methods_rejected:
            a(f"  - `{hit.paragraph.id}` score {hit.score} · "
              + "、".join(f"`{m}`" for m in hit.matched[:6]))
    if diag.dropped_citing:
        a(f"- 因上限略过的引用段：{', '.join(f'`{i}`' for i in diag.dropped_citing)}")
    a("")
    return "\n".join(L) + "\n"
