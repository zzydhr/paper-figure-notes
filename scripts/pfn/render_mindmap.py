"""L4 · 思维导图渲染 —— `PaperBundle` → Mermaid / Markmap HTML / OPML。

纯函数：只吃 `PaperBundle`，不读 PDF、不调模型、不看时钟，
同样的输入永远产出逐字节相同的输出（OPML 刻意不写 `dateCreated`）。

三个层级（PLAN.md §5）::

    ① 单篇证据链导图   mindmap.md        论文 → 图·问题 → panel·实验 → 分组 → 结果
    ② 单图分组结构导图 mindmaps/<slug>.md  图 → panel → 自变量 → 各水平/组 → 结果
    ③ 跨文献主题导图   corpus/mindmap.md   主题轴 → 子问题 → 各论文的证据

三种格式::

    mermaid   ```mermaid 代码块内嵌 .md，Obsidian / Typora / Artifact 直接渲染
    markmap   单文件 HTML，可折叠可缩放可搜索，**零外部依赖**（无 CDN、无 fetch）
    opml      纯 XML，导入 XMind / 幕布 继续手工编辑

设计要点
--------
**渲染树与转义分离**。先建一棵与格式无关的 `MindNode` 树（文本已做空白折叠 +
截断），再由各 serializer 施加各自的转义。这样 OPML/HTML 里的 `(` `%` 保持原样，
只有 mermaid 需要的替换才落到 mermaid 上。

**mermaid 转义用等宽全角替身而非实体码**。mermaid mindmap 对 `()[]{}` 敏感
（会被当成节点形状语法）、对 `:` 敏感（`:::class` / `::icon()`）、对 `<>&#`
敏感（HTML label 渲染 + `#NNN;` 实体转义）、对 `%%` 敏感（注释）、对反引号敏感
（会提前闭合外层 ``` 代码块）。`#40;` 这类实体码依赖 mermaid 版本，全角替身则在
任何版本、任何渲染器下都只是普通文字，故选后者。见 `_MERMAID_TABLE`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence
from xml.sax.saxutils import escape as _xml_escape

from pfn.models import (
    Confidence,
    FigureRecord,
    Panel,
    PaperBundle,
    PaperMeta,
    ReviewReason,
    is_reported,
    needs_review,
    review_reasons,
)

__all__ = [
    "render_mindmap",
    "render_corpus_mindmap",
    "render_figure_mindmap",
    "figure_slug",
    "figure_mindmap_relpath",
    "MindNode",
    "build_paper_tree",
    "build_figure_tree",
    "build_corpus_tree",
    "to_mermaid",
    "to_markmap_html",
    "to_opml",
    "clean_node_text",
    "DEPTHS",
    "FORMATS",
    "MAX_NODE_CHARS",
    "MINDMAP_DIR",
]

# ── 常量 ────────────────────────────────────────────────────────────────────

#: 单个自由文本片段的最大字符数，超出截断加 `…`。结构性前缀（`Figure 1` / `⚠` /
#: 角色符号）不计入，因此一个节点最长约 2×该值 + 前缀。
MAX_NODE_CHARS = 40

DEPTHS = ("figure", "panel", "group", "result")
FORMATS = ("mermaid", "markmap", "opml")
DEFAULT_FORMATS = ("mermaid", "markmap")

#: 单图导图片段的存放目录（相对输出目录）。render_notes 引用时请用
#: `figure_mindmap_relpath()`，不要自己拼字符串。
MINDMAP_DIR = "mindmaps"

_DEPTH_RANK = {name: i for i, name in enumerate(DEPTHS)}

_SEP = " · "
_EMPTY = "（无内容）"

#: 组角色 → 前缀符号。空心=参照系，实心=被操纵对象。图例写在 .md 里。
_ROLE_GLYPH = {
    "control": "○ ",
    "baseline": "◎ ",
    "reference": "▷ ",
    "treatment": "● ",
    "ablation": "◆ ",
}

#: 跨文献导图每条轴最多列多少个归并键 / 每个键下最多列多少条证据。
_CORPUS_MAX_KEYS = 30
_CORPUS_MAX_EVIDENCE = 12


# ── 文本清洗与转义 ──────────────────────────────────────────────────────────

# XML 1.0 与 mermaid 都不接受的控制字符（保留 \t \n \r 交给空白折叠处理）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# Python 的 \s 对 str 模式同时匹配 U+2028/U+2029，正好一并折叠掉
_WS_RE = re.compile(r"\s+")

#: mermaid mindmap 会被这些字符破坏语法，统一换成视觉等价的全角替身。
#: 刻意 **不** 动 `*`（`*** p<0.001` 的显著性星号是有意义的信息）、
#: `'`（英文撇号）、`-` `=` `→`（无语法含义）。
_MERMAID_TABLE = str.maketrans(
    {
        "(": "（",
        ")": "）",
        "[": "［",
        "]": "］",
        "{": "｛",
        "}": "｝",
        ":": "：",
        ";": "；",
        "<": "＜",
        ">": "＞",
        '"': "＂",
        "&": "＆",
        "#": "＃",
        "%": "％",
        "`": "＇",
        "\\": "＼",
        "|": "｜",
    }
)

#: 测试用：转义后绝不允许出现在 mermaid 节点文本里的字符。
MERMAID_FORBIDDEN = frozenset('()[]{}:;<>"&#%`\\|')


def _clean(value: Any) -> str:
    """去控制字符 + 折叠空白。格式无关，所有节点文本的第一道工序。"""
    if value is None:
        return ""
    text = value.value if isinstance(value, Confidence) else str(value)
    text = _CONTROL_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def _fit(text: str, limit: int) -> str:
    """按字符（非字节）截断，超长补 `…`。中英混排下 len() 即视觉长度近似。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def clean_node_text(value: Any, limit: int = MAX_NODE_CHARS) -> str:
    """清洗 + 截断单个片段（不含 mermaid 转义）。供其他渲染器复用。"""
    return _fit(_clean(value), limit)


# ── 图题抽取 ────────────────────────────────────────────────────────────────

#: `Fig. 1.` / `Figure 1:` / `FIGURE 1 |` / `Table IV.` / `图 1` 等图号前缀。
#: `num` 是必需捕获组——没捕到图号就**不剥**，避免把 `Figure quality assessment…`
#: 这种以 Figure 开头的正常句子削掉半截。子图字母必须紧贴数字（`1a`），
#: 否则 `Fig. 1 DMC-GF…` 里的 `D` 会被当成子图号吃掉。
_CAPTION_PREFIX_RE = re.compile(
    r"^(?:fig(?:ure)?s?|tab(?:le)?|scheme|图|表)\s*\.?\s*"
    r"(?P<num>\d+[a-zA-Z]?|[IVXLCDM]{1,7}\b)"
    r"\s*(?:[.:：|、．\-–—]\s*)*",
    re.IGNORECASE,
)

#: 句号属于缩写而非句末的常见词。命中则不在此处断句。
_ABBREVIATIONS = (
    "fig", "figs", "e.g", "i.e", "vs", "etc", "cf", "no", "approx", "et al",
    "i.p", "i.v", "s.c", "p.o", "a.u", "s.d", "s.e.m", "min", "max", "ca",
    "resp", "wt", "conc", "dr", "sp", "spp", "std",
)

_SENTENCE_END_RE = re.compile(r"[.。！？!?]")


def _first_sentence(text: str) -> str:
    """取第一句。英文图题里句号陷阱很多，逐个排除：

    - 小数点（`IC50 = 5.148 μM`）——后面紧跟非空白，被下面那条一并挡掉。
      **不能**改成「句号前是数字就跳过」：`…via Keap1/Nrf2. All images…`、
      `…on WMT14. We report…` 里句号前正是数字，那样会整段不断句。
    - 缩写（`e.g.` `Fig.` `et al.` `i.p.`）
    - 句号后没有空白（`U251/U87.Cells`，多半是排版粘连而非断句）
    - 句号后的下一个实字不是大写 / 左括号（`A. baumannii` 这类属名缩写）
    """
    for match in _SENTENCE_END_RE.finditer(text):
        index = match.start()
        if match.group() == ".":
            following = text[index + 1 : index + 2]
            if following and not following.isspace():
                continue
            tail = text[max(0, index - 8) : index].lower()
            if any(tail.endswith(abbr) for abbr in _ABBREVIATIONS):
                continue
            rest = text[index + 1 :].lstrip()
            if rest and not (rest[0].isupper() or rest[0] in "([【（《"):
                continue
        return text[:index].strip()
    return text.strip()


def figure_title(rec: FigureRecord) -> str:
    """从 `caption` 抽出图题（第一句，去掉 `Fig. N.` 前缀）。抽不出则空串。

    图题是读文献时的**定位坐标**，与 `question`（我们对这张图的二次归纳）作用不同，
    所以原文照抄、不翻译。`ContextPack.caption_preamble` 拿不到（那是 L2 的字段，
    `FigureRecord` 上没有），故就地解析。
    """
    caption = _clean(rec.caption)
    if not caption:
        return ""
    match = _CAPTION_PREFIX_RE.match(caption)
    if match and match.group("num"):
        caption = caption[match.end() :]
    return _first_sentence(caption)


def _mermaid_text(text: str) -> str:
    """mermaid mindmap 节点文本转义。见模块 docstring 的「设计要点」。"""
    out = text.translate(_MERMAID_TABLE).strip()
    return out or _EMPTY


def _xml_attr(text: str) -> str:
    """OPML 属性值转义。`_clean` 已去掉 XML 非法控制字符。"""
    return _xml_escape(text, {'"': "&quot;", "'": "&apos;"})


#: 反引号会提前闭合 ``` 围栏，是结构性风险，必须换掉。
_MARKDOWN_TABLE = str.maketrans({"`": "＇"})

#: 只有后面紧跟字母 / `/` / `!` / `?` 的 `<` 才可能被当成 HTML 标签起始。
#: **不能**无差别替换：`p<0.001`、`log2FC < -1` 在生物医学图题里遍地都是，
#: 而图题的用处正是拿去原文 PDF 里 Ctrl+F 定位——改一个字符就搜不到了。
#: 落单的 `>` 不动，它开不了标签；行首的 `>` 会起引用块，但 `_clean` 已折叠换行，
#: 嵌进来的文本不可能出现在行首。
_MD_TAG_RE = re.compile(r"<(?=[A-Za-z/!?])")


def _md_inline(text: str) -> str:
    """把一段原文（图题 / 论文标题）安全地嵌进 markdown 行内。

    只中和真正能破坏结构的两样：反引号与标签起始的 `<`。**刻意不动 `&`**——
    裸 `&` 和 `&lt;` 在 markdown 里都只渲染成文字、形不成标记，替换它反而会让
    `Nrf2 & HMOX1` 这类标题搜不到。也因此不存在「先转 `&` 又把自己刚生成的
    `&lt;` 转成 `&amp;lt;`」那种自咬顺序问题：两条规则的字符集不相交。

    `_clean` 已把换行折叠成空格——caption 里一个 `\\n` 就足以从引用块里逃出去，
    或把标题行截成两行。
    """
    return _MD_TAG_RE.sub("&lt;", _clean(text).translate(_MARKDOWN_TABLE))


# ── 与格式无关的渲染树 ──────────────────────────────────────────────────────


@dataclass
class MindNode:
    """一个导图节点。`text` 已清洗截断；`note` 保留未截断全文供 tooltip / OPML。"""

    text: str
    note: str = ""
    children: list["MindNode"] = field(default_factory=list)

    def add(
        self,
        *parts: Any,
        prefix: str = "",
        limit: int = MAX_NODE_CHARS,
        keep: bool = False,
    ) -> "MindNode":
        """挂一个子节点，返回它（可继续挂子节点）。

        `parts` 里 `not_reported` / 空 / `-` 的片段自动跳过（见 models.is_reported），
        余下片段各自截断后用 ` · ` 连接。`prefix` 是结构性标记（`⚠ ` / `Panel A` /
        角色符号），不参与截断预算。

        `keep=False`（默认，适用于叶子属性节点）：所有片段都没内容时**整个节点不挂**，
        返回一个游离节点让链式调用照常工作。否则 `n: not_reported` 会渲染出
        `样本量` 这种只有前缀的空壳节点。

        `keep=True`（适用于结构节点：图 / panel / 自变量 / 组）：无论如何都挂上，
        退化到只有前缀、再退化到 `（无内容）`——**绝不产生空文本节点**，
        空行会直接破坏 mermaid 缩进语义。
        """
        shown: list[str] = []
        full: list[str] = []
        for part in parts:
            if not is_reported(part):
                continue
            cleaned = _clean(part)
            if not cleaned:
                continue
            full.append(cleaned)
            shown.append(_fit(cleaned, limit))

        text = (prefix + _SEP.join(shown)).strip() if shown else prefix.strip()
        note = (prefix + _SEP.join(full)).strip() if full else text
        node = MindNode(text=text or _EMPTY, note=note or _EMPTY)
        if shown or keep:
            self.children.append(node)
        return node

    def add_node(self, node: "MindNode") -> "MindNode":
        self.children.append(node)
        return node

    def walk(self) -> Iterator["MindNode"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def count(self) -> int:
        return sum(1 for _ in self.walk())

    def to_dict(self) -> dict:
        """紧凑 JSON 形式，供 HTML 内嵌。t=text, n=note, c=children。"""
        out: dict[str, Any] = {"t": self.text}
        if self.note and self.note != self.text:
            out["n"] = self.note
        if self.children:
            out["c"] = [c.to_dict() for c in self.children]
        return out


def _root(*parts: Any, prefix: str = "", limit: int = MAX_NODE_CHARS) -> MindNode:
    """建根节点。mindmap 只允许一个根，全流程只在这里造。"""
    holder = MindNode(text="")
    return holder.add(*parts, prefix=prefix, limit=limit, keep=True)


# ── serializer: mermaid ─────────────────────────────────────────────────────


def to_mermaid(root: MindNode, indent: int = 2) -> str:
    """渲染成 mermaid `mindmap` 源码（不含 ``` 围栏）。

    层级完全由缩进决定：根缩进 1 级，每深一层加 `indent` 个空格。
    只输出一个根节点——多根会让 mermaid 直接报错。
    """
    lines = ["mindmap"]

    def walk(node: MindNode, level: int) -> None:
        lines.append(" " * (indent * (level + 1)) + _mermaid_text(node.text))
        for child in node.children:
            walk(child, level + 1)

    walk(root, 0)
    return "\n".join(lines)


def mermaid_block(root: MindNode, indent: int = 2) -> str:
    """带 ``` 围栏的 mermaid 代码块，可直接拼进 .md。"""
    return "```mermaid\n" + to_mermaid(root, indent) + "\n```"


# ── serializer: markmap 单文件 HTML ─────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#fbfbfa; --fg:#1f2328; --muted:#6b7280; --line:#d8d8d4;
  --row:#ffffff; --row-hover:#f0f0ee; --accent:#b45309; --badge:#eeeeea;
  --d0:#7c3aed; --d1:#b45309; --d2:#0f766e; --d3:#1d4ed8; --d4:#9d174d; --d5:#4d7c0f;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#16171a; --fg:#e8e6e3; --muted:#9aa0a6; --line:#33353a;
    --row:#1e2024; --row-hover:#282b31; --accent:#f0a868; --badge:#2b2e34;
    --d0:#c4b5fd; --d1:#fcd34d; --d2:#5eead4; --d3:#93c5fd; --d4:#f9a8d4; --d5:#bef264;
  }
}
:root[data-theme="light"]{
  --bg:#fbfbfa; --fg:#1f2328; --muted:#6b7280; --line:#d8d8d4;
  --row:#ffffff; --row-hover:#f0f0ee; --accent:#b45309; --badge:#eeeeea;
  --d0:#7c3aed; --d1:#b45309; --d2:#0f766e; --d3:#1d4ed8; --d4:#9d174d; --d5:#4d7c0f;
}
:root[data-theme="dark"]{
  --bg:#16171a; --fg:#e8e6e3; --muted:#9aa0a6; --line:#33353a;
  --row:#1e2024; --row-hover:#282b31; --accent:#f0a868; --badge:#2b2e34;
  --d0:#c4b5fd; --d1:#fcd34d; --d2:#5eead4; --d3:#93c5fd; --d4:#f9a8d4; --d5:#bef264;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",
  "PingFang SC","Hiragino Sans GB",sans-serif;}
header{position:sticky;top:0;z-index:5;background:var(--bg);
  border-bottom:1px solid var(--line);padding:14px 20px 10px;}
h1{margin:0 0 2px;font-size:16px;font-weight:650;letter-spacing:.01em}
.sub{color:var(--muted);font-size:12.5px;margin-bottom:10px}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
button{font:inherit;font-size:12.5px;padding:4px 10px;border:1px solid var(--line);
  border-radius:6px;background:var(--row);color:var(--fg);cursor:pointer}
button:hover{background:var(--row-hover)}
input[type=search]{font:inherit;font-size:12.5px;padding:4px 10px;flex:1 1 180px;
  min-width:140px;border:1px solid var(--line);border-radius:6px;
  background:var(--row);color:var(--fg)}
main{padding:16px 20px 60px;overflow-x:auto}
ul{list-style:none;margin:0;padding:0 0 0 20px}
#tree{padding-left:0;transform-origin:0 0}
li{position:relative}
li>ul{border-left:1px solid var(--line);margin-left:8px}
li.collapsed>ul{display:none}
li.hidden{display:none}
.row{display:flex;align-items:flex-start;gap:6px;padding:3px 8px;margin:1px 0;
  border-radius:6px;cursor:default;max-width:min(72ch,100%)}
li.has-children>.row{cursor:pointer}
.row:hover{background:var(--row-hover)}
.toggle{flex:0 0 auto;width:14px;color:var(--muted);font-size:11px;
  line-height:1.55;user-select:none;text-align:center}
.label{flex:1 1 auto;word-break:break-word}
.count{flex:0 0 auto;color:var(--muted);font-size:11px;background:var(--badge);
  border-radius:9px;padding:0 6px;line-height:1.7}
.hit{background:rgba(240,168,104,.28);border-radius:3px}
li.d0>.row>.label{font-weight:650;font-size:16px;color:var(--d0)}
li.d1>.row>.label{font-weight:600;color:var(--d1)}
li.d2>.row>.label{color:var(--d2)}
li.d3>.row>.label{color:var(--d3)}
li.d4>.row>.label{color:var(--d4)}
li.d5>.row>.label{color:var(--d5)}
li.d6>.row>.label{color:var(--muted)}
footer{color:var(--muted);font-size:12px;padding:0 20px 30px}
</style>
</head>
<body>
<header>
  <h1>__HEADING__</h1>
  <div class="sub">__SUBTITLE__</div>
  <div class="bar">
    <button id="expand">展开全部</button>
    <button id="collapse">折叠全部</button>
    <button id="zoom-out">缩小</button>
    <button id="zoom-in">放大</button>
    <button id="zoom-reset">100%</button>
    <button id="theme">明/暗</button>
    <input type="search" id="filter" placeholder="搜索节点…（自动展开命中路径）">
    <span class="count" id="stat"></span>
  </div>
</header>
<main><ul id="tree"></ul></main>
<footer>由 paper-figure-notes 生成 · 单文件自包含，无外部依赖，可离线打开</footer>
<script type="application/json" id="tree-data">__DATA__</script>
<script>
(function(){
  "use strict";
  var COLLAPSE_FROM = __COLLAPSE_FROM__;
  var data = JSON.parse(document.getElementById("tree-data").textContent);
  var treeEl = document.getElementById("tree");
  var total = 0;

  function build(node, depth){
    total++;
    var li = document.createElement("li");
    li.className = "node d" + (depth > 6 ? 6 : depth);
    var row = document.createElement("div");
    row.className = "row";
    var toggle = document.createElement("span");
    toggle.className = "toggle";
    var label = document.createElement("span");
    label.className = "label";
    label.textContent = node.t;
    if (node.n && node.n !== node.t) { row.title = node.n; }
    row.appendChild(toggle);
    row.appendChild(label);
    li.appendChild(row);

    var kids = node.c || [];
    if (kids.length) {
      li.classList.add("has-children");
      var ul = document.createElement("ul");
      for (var i = 0; i < kids.length; i++) { ul.appendChild(build(kids[i], depth + 1)); }
      li.appendChild(ul);
      var badge = document.createElement("span");
      badge.className = "count";
      badge.textContent = String(kids.length);
      row.appendChild(badge);
      row.addEventListener("click", function(ev){
        ev.stopPropagation();
        setCollapsed(li, !li.classList.contains("collapsed"));
      });
      setCollapsed(li, depth >= COLLAPSE_FROM);
    } else {
      toggle.textContent = "·";
    }
    return li;
  }

  function setCollapsed(li, collapsed){
    if (!li.classList.contains("has-children")) { return; }
    li.classList.toggle("collapsed", collapsed);
    li.querySelector(".toggle").textContent = collapsed ? "\\u25b8" : "\\u25be";
  }

  treeEl.appendChild(build(data, 0));
  document.getElementById("stat").textContent = total + " 节点";

  function all(sel){ return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  document.getElementById("expand").addEventListener("click", function(){
    all("li.has-children").forEach(function(li){ setCollapsed(li, false); });
  });
  document.getElementById("collapse").addEventListener("click", function(){
    all("li.has-children").forEach(function(li){ setCollapsed(li, li !== treeEl.firstChild); });
  });

  var scale = 1;
  function applyScale(){ treeEl.style.transform = scale === 1 ? "" : "scale(" + scale + ")"; }
  document.getElementById("zoom-in").addEventListener("click", function(){
    scale = Math.min(2.5, scale + 0.15); applyScale();
  });
  document.getElementById("zoom-out").addEventListener("click", function(){
    scale = Math.max(0.4, scale - 0.15); applyScale();
  });
  document.getElementById("zoom-reset").addEventListener("click", function(){
    scale = 1; applyScale();
  });

  document.getElementById("theme").addEventListener("click", function(){
    var cur = document.documentElement.getAttribute("data-theme");
    var dark = cur ? cur === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.setAttribute("data-theme", dark ? "light" : "dark");
  });

  var filterEl = document.getElementById("filter");
  filterEl.addEventListener("input", function(){
    var q = filterEl.value.trim().toLowerCase();
    var items = all("li.node");
    if (!q) {
      items.forEach(function(li){
        li.classList.remove("hidden");
        li.querySelector(".label").classList.remove("hit");
      });
      document.getElementById("stat").textContent = total + " 节点";
      return;
    }
    var hits = 0;
    items.forEach(function(li){
      var label = li.querySelector(".label");
      var match = label.textContent.toLowerCase().indexOf(q) !== -1;
      label.classList.toggle("hit", match);
      li.classList.add("hidden");
      if (match) {
        hits++;
        var cur = li;
        while (cur && cur.classList && cur.classList.contains("node")) {
          cur.classList.remove("hidden");
          if (cur !== li) { setCollapsed(cur, false); }
          cur = cur.parentElement ? cur.parentElement.parentElement : null;
        }
      }
    });
    document.getElementById("stat").textContent = hits + " / " + total + " 命中";
  });
})();
</script>
</body>
</html>
"""


def to_markmap_html(
    root: MindNode,
    title: str = "思维导图",
    subtitle: str = "",
    collapse_from: int = 3,
) -> str:
    """渲染成**自包含**单文件 HTML：可折叠 / 可缩放 / 可搜索。

    刻意不引 markmap/d3 的 CDN——用户可能离线，且 Artifact 的 CSP 会拦外部请求。
    这里是原生 DOM 实现的可折叠树，无任何第三方依赖。

    数据以 `<script type="application/json">` 内嵌，`<` `>` `&` 全部转成 `\\uXXXX`，
    因此节点文本里出现 `</script>` 也无法逃逸；渲染走 `textContent`，天然免疫
    HTML 注入，故此处**不**做 mermaid 那套全角替换，`(` `%` 保持原样。
    """
    payload = json.dumps(root.to_dict(), ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        _HTML_TEMPLATE.replace("__DATA__", payload)
        .replace("__COLLAPSE_FROM__", str(int(collapse_from)))
        .replace("__TITLE__", _html_text(title))
        .replace("__HEADING__", _html_text(title))
        .replace("__SUBTITLE__", _html_text(subtitle))
    )


def _html_text(text: str) -> str:
    return _xml_escape(_clean(text))


# ── serializer: OPML ────────────────────────────────────────────────────────


def to_opml(root: MindNode, title: str = "思维导图") -> str:
    """渲染成 OPML 2.0，可导入 XMind / 幕布 / Workflowy。

    刻意不写 `dateCreated`：本模块是纯函数，输出必须可复现、可 diff。
    截断过的节点把全文放进 `_note`（幕布/Workflowy 约定，XMind 忽略未知属性）。
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        f"    <title>{_xml_attr(_clean(title))}</title>",
        "  </head>",
        "  <body>",
    ]

    def walk(node: MindNode, level: int) -> None:
        pad = "    " + "  " * level
        attrs = f'text="{_xml_attr(node.text)}"'
        if node.note and node.note != node.text:
            attrs += f' _note="{_xml_attr(node.note)}"'
        if node.children:
            lines.append(f"{pad}<outline {attrs}>")
            for child in node.children:
                walk(child, level + 1)
            lines.append(f"{pad}</outline>")
        else:
            lines.append(f"{pad}<outline {attrs}/>")

    walk(root, 0)
    lines += ["  </body>", "</opml>", ""]
    return "\n".join(lines)


# ── 树构建 ──────────────────────────────────────────────────────────────────


def _rank(depth: str) -> int:
    try:
        return _DEPTH_RANK[depth]
    except KeyError:
        raise ValueError(
            f"未知的 mindmap depth: {depth!r}，可选 {'/'.join(DEPTHS)}"
        ) from None


def _panel_prefix(panel: Panel, flagged: frozenset[str] = frozenset()) -> str:
    """`⚠ Panel A · `；整图不分子图（label 为 `-`）时不带标号。

    `flagged` 是被 `review_reasons()` 点名的 panel label 集合——它同时覆盖了自报推断
    与哨兵两路，比 `needs_review()` 严。只有拿不到 reasons 时才退回自报判断。
    """
    warn = "⚠ " if (panel.label in flagged or needs_review(panel)) else ""
    return f"{warn}Panel {_clean(panel.label)}{_SEP}" if is_reported(panel.label) else warn


#: 待确认分枝里各类理由的展示标签与排序。哨兵排最前——它是独立校验的结果，
#: 其余两路都是模型自报，而自报恰恰是最不可信的一环。
_REASON_LABEL = {"sentinel": "哨兵", "inferred": "推断", "low_confidence": "低置信"}
_REASON_ORDER = {"sentinel": 0, "inferred": 1, "low_confidence": 2}


def _self_reported_reasons(rec: FigureRecord) -> list[ReviewReason]:
    """只用 `FigureRecord` 自身能给出的复核理由，形状与 `models.review_reasons()` 一致。

    **这条路径少了校验哨兵那一路**，会漏掉「谎称 `inferred: []` 的脑补记录」——
    仅在拿不到 `PaperBundle` 时兜底。有 bundle 就必须走 `models.review_reasons()`。
    """
    reasons: list[ReviewReason] = []
    for panel in rec.panels:
        if needs_review(panel):
            reasons.append(
                ReviewReason(
                    kind="inferred",
                    panel=panel.label,
                    text="含模型推断字段：" + "、".join(panel.evidence.inferred),
                )
            )
    if rec.confidence == Confidence.LOW:
        reasons.append(ReviewReason(kind="low_confidence", text="整图置信度自评为低"))
    return reasons


def _flagged_panels(reasons: Sequence[ReviewReason]) -> frozenset[str]:
    """哪些 panel 被点名了。`panel is None` 的是图级理由，不落到任何 panel 上。"""
    return frozenset(r.panel for r in reasons if r.panel)


def _group_node(parent: MindNode, group: Any, rank: int) -> MindNode:
    """一个实验组。group 层折叠成「组名 → 结果」；result 层拆成子节点。"""
    glyph = _ROLE_GLYPH.get(str(getattr(group, "role", "")), "· ")
    if rank >= 3:
        node = parent.add(group.name, prefix=glyph, keep=True)
        # 这些一律 keep=False：原文没写就整条不出现，而不是渲染出 `样本量` 空壳。
        # not_reported 的缺席本身就是信息，见 PLAN.md「两条铁律」。
        node.add(group.condition, prefix="条件 ")
        node.add(group.n, prefix="样本量 ")
        node.add(group.readout, prefix="指标 ")
        node.add(group.result, prefix="→ ")
        node.add(group.vs_control, prefix="Δ ")
        return node
    if is_reported(group.result):
        return parent.add(group.name, group.result, prefix=glyph, keep=True)
    return parent.add(group.name, group.condition, prefix=glyph, keep=True)


def build_paper_tree(bundle: PaperBundle, depth: str = "group") -> MindNode:
    """① 单篇证据链导图：论文 → 图·问题 → panel·实验 → 分组 → 结果。"""
    rank = _rank(depth)
    meta = bundle.paper
    # 根节点放宽到 60 字：它是唯一的中心节点，标题截太狠就认不出是哪篇了
    root = _root(meta.title or "本篇论文", limit=60)

    if not bundle.figures:
        root.add("（该 bundle 未包含任何 figure）")
        return root

    # 图级 ⚠ 一律以 review_reasons 为准：它额外并入了 merge 阶段哨兵的 [复核] 警告。
    # 只看 panel 自报的 evidence.inferred 会漏掉最危险的一类——脑补了 n / stats.test
    # 却同时谎称 inferred 为空的记录，那种记录在导图里反而显得比诚实抽取的还干净。
    reasons = review_reasons(bundle)

    for rec in bundle.figures:
        rec_reasons = reasons.get(rec.figure_id, [])
        fig = root.add(
            rec.figure_id,
            rec.question,
            prefix="⚠ " if rec_reasons else "",
            keep=True,
        )
        if rank < 1:
            continue

        flagged = _flagged_panels(rec_reasons)
        if not rec.panels:
            # schematic 等无实验分组的图：给一个说明节点，避免空枝
            fig.add(_FIGURE_KIND_NOTE.get(rec.figure_kind, "无分组实验数据"))
        else:
            for panel in rec.panels:
                pnode = fig.add(
                    panel.experiment, prefix=_panel_prefix(panel, flagged), keep=True
                )
                if rank >= 2:
                    for group in panel.groups:
                        _group_node(pnode, group, rank)
                    pnode.add(panel.finding, prefix="✅ ")

        fig.add(rec.figure_conclusion, prefix="✅ ")

    return root


#: 无 panel 的图按 figure_kind 给一句说明，保证不出现空枝。
_FIGURE_KIND_NOTE = {
    "schematic": "📐 机制示意图（无实验分组）",
    "qualitative_example": "🖼 定性示例（无定量分组）",
    "analysis": "📊 分析型图（未拆分 panel）",
    "experiment": "无 panel 记录",
}


def build_figure_tree(
    rec: FigureRecord,
    depth: str = "group",
    reasons: Optional[Sequence[ReviewReason]] = None,
) -> MindNode:
    """② 单图分组结构导图：图 → panel → 自变量 → 各水平/组 → 结果。

    `depth` 下限为 `panel`——这类片段本就是给「回忆某张图怎么设计的」用的，
    退化成单个根节点没有意义。

    :param reasons: 该图需人工复核的理由，直接传 `models.review_reasons(bundle)[figure_id]`。
        带 `panel` 的理由会把 ⚠ 精确打到对应 panel 节点上。
        传空列表表示「已核对过，确实不需要复核」。**传 None 表示调用方拿不到 bundle**，
        此时退化成只看 `FigureRecord` 自报的信息，会漏掉校验哨兵抓到的脑补记录——
        有 bundle 就一定要传，别用默认值。
    """
    rank = max(_rank(depth), 1)
    if reasons is None:
        reasons = _self_reported_reasons(rec)
    # 根节点用**图题**而非 question：读文献时图题是定位坐标，question 是我们的二次归纳。
    # 图题放宽到 60 字（英文图题普遍偏长），question 降为子节点；抽不出图题才退回 question。
    title = figure_title(rec)
    root = _root(
        rec.figure_id, title or rec.question, prefix="⚠ " if reasons else "", limit=60
    )
    if title:
        root.add(rec.question, prefix="❓ ")
    flagged = _flagged_panels(reasons)

    if not rec.panels:
        root.add(_FIGURE_KIND_NOTE.get(rec.figure_kind, "无分组实验数据"))
        root.add(rec.figure_conclusion, prefix="✅ ")
        _add_pending(root, rec, reasons, rank)
        return root

    for panel in rec.panels:
        pnode = root.add(
            panel.experiment, prefix=_panel_prefix(panel, flagged), keep=True
        )
        if rank >= 2:
            design = panel.design
            # 自变量是这张图的分组依据，groups 就是它的各个水平。
            # keep=True：自变量本身没记录时也得留着这层，否则整个分组子树被丢掉
            axis = pnode.add(design.independent_var, prefix="🎚 自变量 ", keep=True)
            if panel.groups:
                for group in panel.groups:
                    _group_node(axis, group, rank)
            else:
                for level in design.levels:
                    axis.add(level, prefix="水平 ")
            if not axis.children:
                axis.add("（未记录具体分组）")

            if rank >= 3:
                # levels 是设计的各个水平轴（如「浓度 × 时长」拆开后的取值），
                # 与下面具体的分组节点有重叠，所以合成一个节点、且只在最深一层出现
                pnode.add("、".join(_clean(v) for v in design.levels), prefix="设计水平 ", limit=60)
                pnode.add(design.unit, prefix="实验单位 ")
                for item in design.controlled:
                    pnode.add(item, prefix="固定 ")
                stats = panel.stats
                pnode.add(stats.test, prefix="统计检验 ")
                pnode.add(stats.error_bar, prefix="误差棒 ")
                pnode.add(stats.replicates, prefix="重复 ")
                pnode.add(stats.significance, prefix="显著性 ")

            pnode.add(panel.finding, prefix="✅ ")

    root.add(rec.figure_conclusion, prefix="✅ ")
    _add_pending(root, rec, reasons, rank)
    return root


def _add_pending(
    root: MindNode, rec: FigureRecord, reasons: Sequence[ReviewReason], rank: int
) -> None:
    """挂「⚠ 待确认」分枝：复核理由 + 原文未交代的开放问题。

    复核理由排在开放问题前面——哨兵抓到的「填了 n 却没溯源」是可操作的具体指控，
    比笼统的 open_questions 更该先看到。理由按 `kind` 排序（哨兵 → 推断 → 低置信），
    并把 panel 归属写进行内，这样不用回头对照 panel 节点就知道该核哪一个。
    理由放宽到 60 字，截太狠就看不出要核什么了。
    """
    if rank < 2 or not (reasons or rec.open_questions):
        return
    pending = root.add("待确认", prefix="⚠ ")
    # sorted 是稳定排序，同类内保持原顺序，输出仍可复现
    for reason in sorted(reasons, key=lambda r: _REASON_ORDER.get(r.kind, 9)):
        label = _REASON_LABEL.get(reason.kind, reason.kind)
        site = f"Panel {_clean(reason.panel)}{_SEP}" if is_reported(reason.panel) else ""
        pending.add(reason.text, prefix=f"{label}{_SEP}{site}", limit=60)
    for question in rec.open_questions:
        pending.add(question)


def _short_paper(meta: PaperMeta) -> str:
    """📚 论文清单里的一行：标题 + 年份 + 期刊。"""
    bits = [_fit(_clean(meta.title) or "未命名论文", 40)]
    if meta.year:
        bits.append(str(meta.year))
    if is_reported(meta.venue):
        bits.append(_fit(_clean(meta.venue), 20))
    return _SEP.join(bits)


def _merge_key(text: str) -> str:
    """归并键：小写 + 去空白与常见标点。只做精确归并，不做语义聚类。"""
    return re.sub(r"[\s，。、,.;；:：·\-–—/()（）]+", "", _clean(text).lower())


_Bucket = dict[str, tuple[str, list[tuple[str, ...]]]]


def _axis(parent: MindNode, title: str, buckets: _Bucket) -> None:
    """把一条归并轴挂到跨文献导图上。按证据条数降序，命中多的浮到前面。"""
    node = parent.add(title, keep=True)
    if not buckets:
        node.add("（无可归并的记录）")
        return
    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1][1]), kv[0]))
    for _, (display, evidence) in ordered[:_CORPUS_MAX_KEYS]:
        key_node = node.add(display, keep=True)
        for parts in evidence[:_CORPUS_MAX_EVIDENCE]:
            key_node.add(*parts, keep=True)
        if len(evidence) > _CORPUS_MAX_EVIDENCE:
            key_node.add(f"…另有 {len(evidence) - _CORPUS_MAX_EVIDENCE} 条")
    if len(ordered) > _CORPUS_MAX_KEYS:
        node.add(f"…另有 {len(ordered) - _CORPUS_MAX_KEYS} 项未展开")


def build_corpus_tree(bundles: list[PaperBundle]) -> MindNode:
    """③ 跨文献主题导图：三条归并轴 → 子问题 → 各论文的证据。

    没有模型参与，所以不做语义聚类，只按**规范化后精确相同**的键归并
    （`_merge_key`）。归得上的是真重合，归不上的各自成枝——宁可散，不要假聚类。
    """
    n_fig = sum(len(b.figures) for b in bundles)
    n_group = sum(
        len(p.groups) for b in bundles for f in b.figures for p in f.panels
    )
    root = _root(f"跨文献汇总 · {len(bundles)} 篇 / {n_fig} 图 / {n_group} 组", limit=60)

    if not bundles:
        root.add("（没有可汇总的论文）")
        return root

    # 用 P1/P2… 短引用键指代论文：证据行前面挂全标题会把 40 字预算全吃掉，
    # 真正的信息（哪张图、什么结果）反而被截没。全称在 📚 论文清单里对照。
    # 与单篇导图同源：⚠ 一律以 review_reasons 为准，含 merge 哨兵的 [复核] 警告。
    # 写综述时最忌讳把没核过的数字抄进表里，跨文献视图更需要这个标记。
    marks = [review_reasons(b) for b in bundles]

    papers = root.add("📚 论文清单", keep=True)
    paper_ids: list[str] = []
    for index, (bundle, mark) in enumerate(zip(bundles, marks), 1):
        pid = f"P{index}"
        paper_ids.append(pid)
        pnode = papers.add(_short_paper(bundle.paper), prefix=f"{pid}{_SEP}", limit=80, keep=True)
        pnode.add(f"{len(bundle.figures)} 张图 · domain {bundle.domain}")
        if mark:
            pnode.add(f"{len(mark)} 张图需人工复核", prefix="⚠ ")
        pnode.add(bundle.paper.doi, prefix="DOI ", limit=60)

    by_question: _Bucket = {}
    by_var: _Bucket = {}
    by_readout: _Bucket = {}

    def bucket(store: _Bucket, raw: str, *parts: str) -> None:
        key = _merge_key(raw)
        if not key:
            return
        store.setdefault(key, (_fit(_clean(raw), MAX_NODE_CHARS), []))[1].append(parts)

    for pid, bundle, mark in zip(paper_ids, bundles, marks):
        for rec in bundle.figures:
            # ⚠ 前缀直接进证据行：跨文献视图里每一行都是独立引用的，
            # 光在论文清单上标一次，抄数字的人不会回头看
            flag = "⚠ " if mark.get(rec.figure_id) else ""
            if is_reported(rec.question):
                bucket(
                    by_question,
                    rec.question,
                    f"{flag}{pid}{_SEP}{rec.figure_id}",
                    rec.figure_conclusion if is_reported(rec.figure_conclusion) else "结论未记录",
                )
            for panel in rec.panels:
                site = f"{flag}{pid}{_SEP}{rec.figure_id}"
                if is_reported(panel.label):
                    site += f"/{_clean(panel.label)}"
                if is_reported(panel.design.independent_var):
                    levels = "、".join(_clean(v) for v in panel.design.levels[:4])
                    bucket(by_var, panel.design.independent_var, site, levels or "水平未记录")
                for group in panel.groups:
                    if not is_reported(group.readout):
                        continue
                    bucket(
                        by_readout,
                        group.readout,
                        site,
                        group.name,
                        group.result if is_reported(group.result) else "结果未记录",
                    )

    _axis(root, "❓ 按研究问题", by_question)
    _axis(root, "🎚 按干预 / 自变量", by_var)
    _axis(root, "📏 按测量指标", by_readout)
    return root


# ── 文件名约定 ──────────────────────────────────────────────────────────────

_FIG_ID_RE = re.compile(r"^([A-Za-z一-鿿]+)[\s.]*([0-9]+)([A-Za-z]?)$")


def figure_slug(figure_id: str) -> str:
    """`"Figure 1"` → `"figure_01"`；`"Table 3"` → `"table_03"`。

    数字补零到两位，保证文件名字典序 == 图号序。
    """
    text = _clean(figure_id)
    match = _FIG_ID_RE.match(text)
    if match:
        word, number, suffix = match.groups()
        return f"{word.lower()}_{int(number):02d}{suffix.lower()}"
    slug = re.sub(r"[^0-9A-Za-z一-鿿]+", "_", text).strip("_").lower()
    return slug or "figure"


def figure_mindmap_relpath(figure_id: str) -> str:
    """单图导图片段相对输出目录的路径，如 `mindmaps/figure_01.md`。

    **给 render_notes 用**：要在笔记里链接/嵌入某图的导图，请调用本函数，
    不要自己拼字符串——命名规则变了这里会跟着变。
    """
    return f"{MINDMAP_DIR}/{figure_slug(figure_id)}.md"


def render_figure_mindmap(
    rec: FigureRecord,
    depth: str = "group",
    reasons: Optional[Sequence[ReviewReason]] = None,
) -> str:
    """② 单图导图，返回**带 ``` 围栏的 mermaid 代码块**，可直接嵌进精读笔记。

    给 render_notes 的免落盘入口：不写文件、不碰磁盘，纯字符串。

    调用方手上有 `PaperBundle` 时**务必**传 `reasons`，否则 ⚠ 会漏标::

        from pfn.models import review_reasons
        marks = review_reasons(bundle)
        block = render_figure_mindmap(rec, depth, reasons=marks.get(rec.figure_id, []))

    带 `panel` 的理由会让 ⚠ 精确落到对应 panel 节点，而不只是整图打个标。
    """
    return mermaid_block(build_figure_tree(rec, depth, reasons))


# ── 落盘 ────────────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> Path:
    """统一 UTF-8 + LF 写盘。Windows 默认 GBK / CRLF，这里必须显式指定。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _check_formats(formats: Optional[Iterable[str]]) -> list[str]:
    chosen = list(formats) if formats else list(DEFAULT_FORMATS)
    unknown = [f for f in chosen if f not in FORMATS]
    if unknown:
        raise ValueError(
            f"未知的 mindmap format: {unknown}，可选 {'/'.join(FORMATS)}"
        )
    # 保持 FORMATS 的固定顺序，产物列表才稳定可比
    return [f for f in FORMATS if f in chosen]


_LEGEND = (
    "> **图例**　⚠ 需人工复核（低置信 / 含模型推断字段 / **校验哨兵存疑**）　✅ 结论　"
    "🎚 自变量　→ 结果　Δ 相对对照\n"
    "> 　　　　　● 处理组　○ 对照组　◎ baseline　◆ 消融　▷ 参照\n"
    ">\n"
    "> ⚠ **精确到 panel**：理由若点名了某个 panel，只有那个 panel 节点带 ⚠，"
    "多联图里不必逐个排查；图级理由则只标在图节点上。\n"
    "> 哨兵存疑指该图填了 `n` / 统计检验等高危字段却没在 `evidence` 里溯源。"
    "自报的 `inferred: []` 不能单独作为「无需复核」的依据——脑补的记录往往正好谎称零推断。"
)


def _paper_heading(meta: PaperMeta) -> str:
    """.md 抬头。标题 / 期刊 / DOI 都是原文，同样得中和 markdown 结构字符。"""
    bits = [
        _md_inline(b)
        for b in (str(meta.year) if meta.year else "", meta.venue)
        if is_reported(b)
    ]
    line = f"# 证据链导图 · {_md_inline(meta.title) or '未命名论文'}"
    if bits:
        line += f"\n\n{_SEP.join(bits)}"
    if is_reported(meta.doi):
        line += f"　·　DOI {_md_inline(meta.doi)}"
    return line


def render_mindmap(
    bundle: PaperBundle,
    out_dir: Path,
    depth: str = "group",
    formats: Optional[list[str]] = None,
) -> list[Path]:
    """渲染单篇论文的思维导图，返回所有产物路径。

    产物::

        <out>/mindmap.md            ① 证据链导图（mermaid）
        <out>/mindmap.html          ① 可折叠 HTML（markmap）
        <out>/mindmap.opml          ① OPML（导 XMind / 幕布）
        <out>/mindmaps/<slug>.md    ② 每图一个片段，供笔记引用（仅 mermaid 格式）

    :param depth:   `figure` / `panel` / `group`（默认）/ `result`，逐层展开
    :param formats: `mermaid` / `markmap` / `opml` 的子集，默认前两者
    """
    _rank(depth)  # 提前校验，别等写了一半才报错
    chosen = _check_formats(formats)
    out_dir = Path(out_dir)

    tree = build_paper_tree(bundle, depth)
    title = _clean(bundle.paper.title) or "未命名论文"
    written: list[Path] = []

    if "mermaid" in chosen:
        body = "\n\n".join(
            [
                _paper_heading(bundle.paper),
                f"> 展开层级 `{depth}`　·　{tree.count()} 节点"
                f"　·　{len(bundle.figures)} 张图",
                _LEGEND,
                mermaid_block(tree),
                _siblings_note(chosen, bundle.figures),
            ]
        )
        written.append(_write(out_dir / "mindmap.md", body.rstrip() + "\n"))

    if "markmap" in chosen:
        written.append(
            _write(
                out_dir / "mindmap.html",
                to_markmap_html(
                    tree,
                    title=f"证据链导图 · {title}",
                    subtitle=f"展开层级 {depth}　·　{tree.count()} 节点"
                    f"　·　{len(bundle.figures)} 张图　·　"
                    f"⚠ 需复核　✅ 结论　● 处理　○ 对照",
                ),
            )
        )

    if "opml" in chosen:
        written.append(
            _write(out_dir / "mindmap.opml", to_opml(tree, title=f"证据链导图 · {title}"))
        )

    # ② 单图片段。.md 供 render_notes 嵌进笔记；.html 供单图视角单独浏览
    if "mermaid" in chosen or "markmap" in chosen:
        marks = review_reasons(bundle)
        seen: dict[str, int] = {}
        for rec in bundle.figures:
            slug = figure_slug(rec.figure_id)
            seen[slug] = seen.get(slug, 0) + 1
            if seen[slug] > 1:  # 图号撞车（理论上不该发生），加序号兜底
                slug = f"{slug}_{seen[slug]}"
            fig_tree = build_figure_tree(rec, depth, marks.get(rec.figure_id, []))
            heading = _figure_heading(rec)

            if "mermaid" in chosen:
                body = "\n\n".join([heading, _LEGEND, mermaid_block(fig_tree)])
                written.append(_write(out_dir / MINDMAP_DIR / f"{slug}.md", body + "\n"))

            if "markmap" in chosen:
                written.append(
                    _write(
                        out_dir / MINDMAP_DIR / f"{slug}.html",
                        to_markmap_html(
                            fig_tree,
                            title=f"{_clean(rec.figure_id)} · {figure_title(rec) or '实验设计导图'}",
                            subtitle=f"展开层级 {depth}　·　{fig_tree.count()} 节点"
                            f"　·　{len(rec.panels)} 个 panel　·　"
                            f"⚠ 需复核　✅ 结论　● 处理　○ 对照",
                            collapse_from=4,
                        ),
                    )
                )

    return written


def _figure_heading(rec: FigureRecord) -> str:
    """单图片段的 markdown 抬头。图题原文照抄、不翻译——它是定位坐标。

    图题来自 caption 原文，可能含反引号或裸 HTML，直接写进 .md 会破坏结构，
    故走 `_md_inline`。
    """
    title = _md_inline(figure_title(rec))
    line = f"### {_md_inline(rec.figure_id)} · 实验设计导图"
    if title:
        line += f"\n\n> **图题**　{title}"
    return line


def _siblings_note(chosen: list[str], figures: list[FigureRecord]) -> str:
    lines = ["## 其他产物", ""]
    if "markmap" in chosen:
        lines.append("- [`mindmap.html`](mindmap.html) —— 可折叠 / 可缩放 / 可搜索，单文件离线可用")
    if "opml" in chosen:
        lines.append("- [`mindmap.opml`](mindmap.opml) —— 导入 XMind / 幕布 继续手工编辑")
    if figures:
        lines.append(f"- [`{MINDMAP_DIR}/`]({MINDMAP_DIR}/) —— 每张图单独的分组结构导图：")
        for rec in figures:
            rel = figure_mindmap_relpath(rec.figure_id)
            lines.append(f"  - [`{_clean(rec.figure_id)}`]({rel})")
    return "\n".join(lines)


def render_corpus_mindmap(bundles: list[PaperBundle], out_dir: Path) -> Path:
    """③ 跨文献主题导图。返回 `mindmap.md`，同时落 `.html` / `.opml` 兄弟文件。

    契约只允许返回一个 Path，但跨文献导图节点多、必须能折叠才读得下去，
    因此 HTML/OPML 一并生成，并在 md 正文里给出指路链接。
    """
    out_dir = Path(out_dir)
    tree = build_corpus_tree(bundles)
    n_fig = sum(len(b.figures) for b in bundles)

    body = "\n\n".join(
        [
            "# 跨文献主题导图",
            f"> {len(bundles)} 篇论文 · {n_fig} 张图 · {tree.count()} 节点\n"
            f">\n"
            f"> 三条轴分别按**研究问题** / **干预（自变量）** / **测量指标**归并。\n"
            f"> 归并只做规范化后的精确匹配，不做语义聚类——归到一起的是真重合。",
            _LEGEND,
            mermaid_block(tree),
            "## 其他产物\n\n"
            "- [`mindmap.html`](mindmap.html) —— 可折叠 / 可搜索，篇数多时用这个\n"
            "- [`mindmap.opml`](mindmap.opml) —— 导入 XMind / 幕布",
        ]
    )

    path = _write(out_dir / "mindmap.md", body + "\n")
    _write(
        out_dir / "mindmap.html",
        to_markmap_html(
            tree,
            title="跨文献主题导图",
            subtitle=f"{len(bundles)} 篇 · {n_fig} 图 · {tree.count()} 节点",
            collapse_from=2,
        ),
    )
    _write(out_dir / "mindmap.opml", to_opml(tree, title="跨文献主题导图"))
    return path
