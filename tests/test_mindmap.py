"""L4 思维导图渲染的回归测试。

重点不在「跑得通」，而在**生成的 mermaid 语法真的有效**——节点文本里的
`()` `%` `<>` `:` 反引号一旦漏出去就会静默破坏整张图，而且往往只在别人的
Obsidian 里才炸。所以这里自己实现了一个 mindmap 缩进解析器
（`assert_valid_mermaid`），逐行验证根唯一、缩进合法、无空节点、无危险字符。

对抗性用例见 `hostile_bundle()`：样本数据里真实出现过的 `IC50 = 5.148 μM`、
`*** p<0.001`、`Q1-UR(11.49%)`，加上 mermaid 特有的注入面
（`%%{init:...}%%` 指令、`root((circle))` 形状、`:::class`、``` 围栏）。

跑法::

    pytest tests/test_mindmap.py -q
    python tests/test_mindmap.py          # 不依赖 pytest 也能跑
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from pfn.models import (  # noqa: E402
    Confidence,
    Design,
    Evidence,
    FigureRecord,
    Group,
    Panel,
    PaperBundle,
    PaperMeta,
    Stats,
    needs_review,
    review_reasons,
)
from pfn.render_mindmap import (  # noqa: E402
    DEPTHS,
    FORMATS,
    MAX_NODE_CHARS,
    MERMAID_FORBIDDEN,
    MINDMAP_DIR,
    build_corpus_tree,
    build_figure_tree,
    build_paper_tree,
    clean_node_text,
    figure_mindmap_relpath,
    figure_slug,
    figure_title,
    render_corpus_mindmap,
    render_figure_mindmap,
    render_mindmap,
    to_markmap_html,
    to_mermaid,
    to_opml,
)

FIXTURE = _ROOT / "tests" / "fixtures" / "sample_bundle.json"


def load_sample() -> PaperBundle:
    return PaperBundle.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


# ── mermaid mindmap 校验器 ──────────────────────────────────────────────────

#: 节点行最长多少字符。前缀 + 至多 3 个 40 字片段 + 分隔符，留足余量。
MAX_LINE_CHARS = 200


def assert_valid_mermaid(case: unittest.TestCase, source: str, label: str = "") -> int:
    """按 mermaid mindmap 的缩进语义校验源码，返回节点数。

    mindmap 的层级**完全**由缩进决定，因此非法缩进不会报错，只会把节点挂错父亲
    或让整图渲染失败。这里逐条检查真正会出问题的地方。
    """
    where = f"[{label}] " if label else ""
    lines = source.split("\n")
    case.assertEqual(lines[0], "mindmap", f"{where}首行必须是 mindmap 关键字")
    case.assertGreaterEqual(len(lines), 2, f"{where}至少要有一个根节点")

    stack: list[int] = []  # 当前祖先链的缩进量
    roots = 0
    count = 0

    for lineno, line in enumerate(lines[1:], start=2):
        case.assertTrue(line.strip(), f"{where}第 {lineno} 行是空行，会截断 mindmap")
        case.assertEqual(line, line.rstrip(), f"{where}第 {lineno} 行有行尾空白")

        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()
        count += 1

        case.assertNotIn("\t", line, f"{where}第 {lineno} 行含 tab，缩进必须用空格")
        case.assertGreaterEqual(indent, 2, f"{where}第 {lineno} 行缩进不足，根也要缩进")
        case.assertEqual(indent % 2, 0, f"{where}第 {lineno} 行缩进 {indent} 不是 2 的倍数")
        case.assertTrue(text, f"{where}第 {lineno} 行节点文本为空")
        case.assertLessEqual(
            len(text), MAX_LINE_CHARS, f"{where}第 {lineno} 行节点过长：{text[:60]}…"
        )

        leaked = sorted(set(text) & MERMAID_FORBIDDEN)
        case.assertFalse(
            leaked, f"{where}第 {lineno} 行漏出危险字符 {leaked}：{text[:80]}"
        )

        if not stack:
            stack.append(indent)
            roots += 1
            continue

        if indent > stack[-1]:
            case.assertEqual(
                indent,
                stack[-1] + 2,
                f"{where}第 {lineno} 行一次缩进跳了不止一层（{stack[-1]} → {indent}）",
            )
            stack.append(indent)
        else:
            while stack and stack[-1] > indent:
                stack.pop()
            case.assertTrue(
                stack and stack[-1] == indent,
                f"{where}第 {lineno} 行缩进 {indent} 对不上任何祖先层级",
            )
            if len(stack) == 1:
                roots += 1

    case.assertEqual(roots, 1, f"{where}mindmap 只允许一个根节点，实际 {roots} 个")
    return count


def _caption_line(markdown: str) -> str:
    """抠出片段里的 `> **图题**　…` 一行。

    断言 markdown 正文的转义时必须只看正文——同一个文件里的 mermaid 块走的是
    另一套（全角替身）规则，两者本就该不同。
    """
    for line in markdown.split("\n"):
        if line.startswith("> **图题**"):
            return line.split("**图题**", 1)[1].strip("　 ").strip()
    return ""


def assert_caption_line(case: unittest.TestCase, markdown: str, expected: str) -> None:
    """先确认图题行存在，再比对转义结果。

    分两步是有意的。下面几条用例测的是**转义**，但它们必须先由 `figure_title`
    把图题抽出来才有东西可转。抽取一旦坏掉，这几条会跟着红——而如果失败信息只是
    一句 assertEqual 不匹配，读的人会跑去查 `_md_inline`，方向就错了。
    前置断言把「这不是转义的锅」直接写进消息里。

    （变异测试发现的：把 `figure_title` 打断，红的 5 条里有 3 条是转义用例。）
    """
    line = _caption_line(markdown)
    case.assertTrue(
        line,
        "前提失败：片段里没有图题行——是 figure_title 没抽出图题，"
        "不是 _md_inline 转义有问题。本用例测的是转义。",
    )
    case.assertEqual(line, expected)


def mermaid_of(markdown: str) -> str:
    """从 .md 里抠出唯一的 ```mermaid 代码块。"""
    blocks = re.findall(r"```mermaid\n(.*?)\n```", markdown, re.S)
    assert len(blocks) == 1, f"期望恰好一个 mermaid 代码块，实得 {len(blocks)}"
    return blocks[0]


# ── 对抗性样本 ──────────────────────────────────────────────────────────────

#: 每一条都是能真实破坏 mermaid / HTML / XML 的字符串。
HOSTILE = [
    "(A) CCK-8 assay and analysis",          # 圆括号 → 被当成节点形状
    "IC50 = 5.148 μM",                       # 等号 + 希腊字母（必须保留）
    "*** p<0.001",                           # 星号必须保留，< 会被当 HTML 标签
    "Q1-UR(11.49%)",                         # 括号 + 百分号
    "细胞活力（CCK-8, %）下降 50% ± 2.3",      # 中英混排 + 多个百分号
    'a: b; c "d" [e] {f} |g| #h &i \\j `k`',  # 全套语法字符
    "%%{init: {'theme':'dark'}}%%",          # mermaid 指令注入
    "root((circle)) 形状注入",                 # 节点形状注入
    ":::myClass ::icon(fa fa-book)",         # class / icon 语法注入
    "```\n# 假标题\n```",                      # 代码围栏逃逸
    "<script>alert(1)</script>",             # HTML/JS 注入
    "&amp; &#40; &lt; 实体码",                 # 实体码
    "line1\nline2\ttab\r\nline3",            # 换行 / 制表
    "\x00\x01\x1f 控制字符 \x7f",              # 控制字符（XML 非法）
    "行分隔 段分隔 符",               # U+2028/29，会炸 JS 字面量
    "超长中文文本" * 40,                        # 240 字，必须截断
    "A very long English sentence " * 10,     # 长英文
    "   ",                                    # 纯空白
    "-- 破折号开头",
    "→ 箭头 · 中点 ⚠ 警告",
]


def hostile_bundle() -> PaperBundle:
    """把 HOSTILE 里的字符串塞进 schema 的每一个文本字段。"""
    groups = [
        Group(
            name=HOSTILE[i % len(HOSTILE)],
            role=["treatment", "control", "baseline", "ablation", "reference"][i % 5],
            condition=HOSTILE[(i + 1) % len(HOSTILE)],
            n=HOSTILE[(i + 2) % len(HOSTILE)],
            readout=HOSTILE[(i + 3) % len(HOSTILE)],
            result=HOSTILE[(i + 4) % len(HOSTILE)],
            vs_control=HOSTILE[(i + 5) % len(HOSTILE)],
        )
        for i in range(len(HOSTILE))
    ]
    panel = Panel(
        label="(A)",
        experiment=HOSTILE[0],
        design=Design(
            independent_var=HOSTILE[6],
            levels=HOSTILE[:5],
            controlled=HOSTILE[5:9],
            unit=HOSTILE[9],
        ),
        groups=groups,
        stats=Stats(
            test=HOSTILE[2],
            error_bar=HOSTILE[3],
            replicates=HOSTILE[10],
            significance=HOSTILE[2],
        ),
        finding=HOSTILE[11],
        evidence=Evidence(inferred=["groups[].n"]),
    )
    # label 为 "-"（整图不分子图）与空 groups 的退化 panel
    degenerate = Panel(label="-", experiment=HOSTILE[7], groups=[])
    empty = Panel(label="", experiment="", groups=[])

    return PaperBundle(
        paper=PaperMeta(title=HOSTILE[15], year=2026, venue=HOSTILE[5], doi="10.1/x(y)"),
        domain="bio",
        figures=[
            FigureRecord(
                figure_id="Figure 1",
                caption=HOSTILE[9],
                figure_kind="experiment",
                question=HOSTILE[4],
                panels=[panel, degenerate, empty],
                figure_conclusion=HOSTILE[6],
                confidence=Confidence.LOW,
                open_questions=HOSTILE[:3],
            ),
            # schematic：panels 必须为空，且不能产生空枝
            FigureRecord(
                figure_id="Figure 2",
                figure_kind="schematic",
                question=HOSTILE[8],
                panels=[],
                figure_conclusion=HOSTILE[10],
                confidence=Confidence.HIGH,
            ),
            # 所有字段都缺省（not_reported）的极端退化图
            FigureRecord(figure_id="Table 10"),
        ],
    )


# ── 用例 ────────────────────────────────────────────────────────────────────


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class TestTextCleaning(unittest.TestCase):
    """转义与截断——最容易出 bug 的一层，单独测。"""

    def test_truncates_to_limit(self):
        self.assertEqual(len(clean_node_text("超长中文" * 50)), MAX_NODE_CHARS)
        self.assertTrue(clean_node_text("超长中文" * 50).endswith("…"))

    def test_short_text_untouched(self):
        self.assertEqual(clean_node_text("IC50 = 5.148 μM"), "IC50 = 5.148 μM")

    def test_collapses_whitespace_and_controls(self):
        self.assertEqual(clean_node_text("a\n\tb\r\n  c"), "a b c")
        self.assertEqual(clean_node_text("\x00\x01x\x7f"), "x")
        self.assertEqual(clean_node_text("a b c"), "a b c")

    def test_significance_stars_and_units_survive(self):
        """`***` 与 μM 是有意义的信息，转义时必须原样保留。"""
        node = to_mermaid(_tree_of("*** p<0.001", "IC50 = 5.148 μM ± 0.3"))
        self.assertIn("***", node)
        self.assertIn("μM", node)
        self.assertIn("±", node)
        self.assertIn("＜", node)  # < 已被替换
        self.assertNotIn("<", node)

    def test_empty_input_never_yields_empty_node(self):
        for raw in ("", "   ", "\n\t", "not_reported", "-"):
            source = to_mermaid(_tree_of(raw))
            for line in source.split("\n")[1:]:
                self.assertTrue(line.strip(), f"{raw!r} 产生了空节点")


def _tree_of(*texts: str):
    """拿任意文本造一棵最小树，用于单点验证转义。"""
    root = build_paper_tree(
        PaperBundle(
            paper=PaperMeta(title=texts[0] if texts else "t"),
            figures=[
                FigureRecord(
                    figure_id="Figure 1",
                    question=texts[0] if texts else "",
                    panels=[
                        Panel(
                            label="A",
                            experiment=t,
                            groups=[Group(name=t, role="treatment", result=t)],
                        )
                        for t in (texts or ("",))
                    ],
                )
            ],
        ),
        depth="group",
    )
    return root


class TestSampleBundle(TempDirCase):
    """用真实论文样本跑通四种 depth × 三种格式。"""

    def setUp(self) -> None:
        super().setUp()
        self.bundle = load_sample()

    def test_all_depths_all_formats(self):
        for depth in DEPTHS:
            with self.subTest(depth=depth):
                out = self.out / depth
                paths = render_mindmap(self.bundle, out, depth=depth, formats=list(FORMATS))
                names = {p.name for p in paths}
                self.assertIn("mindmap.md", names)
                self.assertIn("mindmap.html", names)
                self.assertIn("mindmap.opml", names)
                for path in paths:
                    self.assertTrue(path.is_file(), path)
                    self.assertGreater(path.stat().st_size, 0, path)
                    path.read_text(encoding="utf-8")  # 必须是合法 UTF-8

                assert_valid_mermaid(
                    self, mermaid_of((out / "mindmap.md").read_text(encoding="utf-8")), depth
                )

    def test_per_figure_fragments_written(self):
        paths = render_mindmap(self.bundle, self.out, depth="group", formats=["mermaid"])
        for rec in self.bundle.figures:
            rel = figure_mindmap_relpath(rec.figure_id)
            path = self.out / rel
            self.assertTrue(path.is_file(), f"缺少单图导图片段 {rel}")
            self.assertIn(path, paths)
            assert_valid_mermaid(
                self, mermaid_of(path.read_text(encoding="utf-8")), rec.figure_id
            )

    def test_default_formats_skip_opml(self):
        paths = render_mindmap(self.bundle, self.out, depth="group")
        self.assertNotIn("mindmap.opml", {p.name for p in paths})
        self.assertIn("mindmap.html", {p.name for p in paths})

    def test_depth_monotonically_expands(self):
        counts = [build_paper_tree(self.bundle, d).count() for d in DEPTHS]
        self.assertEqual(counts, sorted(counts), f"节点数应随 depth 单调不减：{counts}")
        self.assertLess(counts[0], counts[-1])
        # figure 层 = 根 + 每图一个节点
        self.assertEqual(counts[0], 1 + len(self.bundle.figures))

    def test_result_depth_carries_group_details(self):
        source = to_mermaid(build_figure_tree(self.bundle.figures[0], "result"))
        self.assertIn("样本量 3", source)          # n 被记录时才出现
        self.assertIn("显著性 *** 标注于", source)
        self.assertIn("🎚 自变量", source)
        self.assertIn("误差棒", source)

    def test_not_reported_fields_are_omitted_not_stubbed(self):
        """`n: not_reported` 必须整条不出现，而不是渲染成 `样本量` 空壳。"""
        source = to_mermaid(build_figure_tree(self.bundle.figures[0], "result"))
        for line in source.split("\n"):
            self.assertNotRegex(
                line.strip(),
                r"^(样本量|统计检验|重复|显著性|条件|指标)$",
                f"出现了只有前缀的空壳节点：{line!r}",
            )
        self.assertNotIn("not_reported", source)

    def test_warning_marker_on_inferred_panels(self):
        """Figure 1 的 panel A/C 含 inferred 字段 → panel 与整图都要标 ⚠。"""
        source = to_mermaid(build_paper_tree(self.bundle, "panel"))
        self.assertIn("⚠ Figure 1", source)
        self.assertIn("⚠ Panel A", source)
        self.assertIn("⚠ Panel C", source)
        self.assertIn("Panel B", source)
        self.assertNotIn("⚠ Panel B", source)  # panel B 的 inferred 为空

    def test_warning_marker_propagates_to_figure_depth(self):
        """depth=figure 时看不到 panel，⚠ 必须已经冒泡到图节点上。"""
        source = to_mermaid(build_paper_tree(self.bundle, "figure"))
        self.assertIn("⚠ Figure 1", source)
        self.assertIn("⚠ Figure 2", source)
        self.assertNotIn("⚠ Figure 6", source)  # 高置信 + 无 panel

    def test_low_confidence_marks_figure(self):
        bundle = load_sample()
        bundle.figures[2].confidence = Confidence.LOW
        self.assertIn("⚠ Figure 6", to_mermaid(build_paper_tree(bundle, "figure")))

    def test_schematic_figure_renders_without_empty_branch(self):
        """Figure 6 是 schematic，panels 为空——不能产生空枝或语法错误。"""
        rec = self.bundle.figures[2]
        self.assertEqual(rec.figure_kind, "schematic")
        self.assertEqual(rec.panels, [])
        for depth in DEPTHS:
            source = to_mermaid(build_figure_tree(rec, depth))
            count = assert_valid_mermaid(self, source, f"schematic/{depth}")
            self.assertGreaterEqual(count, 2, "schematic 图也该有说明节点")
            self.assertIn("📐", source)

    def test_render_figure_mindmap_returns_fenced_block(self):
        block = render_figure_mindmap(self.bundle.figures[0], "group")
        self.assertTrue(block.startswith("```mermaid\n"))
        self.assertTrue(block.endswith("\n```"))
        assert_valid_mermaid(self, mermaid_of(block), "fragment")

    def test_pure_function_does_not_mutate_bundle(self):
        before = self.bundle.model_dump(mode="json")
        render_mindmap(self.bundle, self.out, depth="result", formats=list(FORMATS))
        self.assertEqual(before, self.bundle.model_dump(mode="json"))

    def test_deterministic_output(self):
        """纯函数：两次渲染必须逐字节相同（OPML 刻意不写时间戳）。"""
        a = render_mindmap(self.bundle, self.out / "a", formats=list(FORMATS))
        b = render_mindmap(self.bundle, self.out / "b", formats=list(FORMATS))
        self.assertEqual(len(a), len(b))
        for pa, pb in zip(a, b):
            self.assertEqual(pa.read_bytes(), pb.read_bytes(), pa.name)

    def test_files_are_utf8_with_lf(self):
        """Windows 默认 GBK + CRLF，必须显式覆盖。"""
        paths = render_mindmap(self.bundle, self.out, formats=list(FORMATS))
        for path in paths:
            raw = path.read_bytes()
            raw.decode("utf-8")  # 非 UTF-8 会抛异常
            self.assertNotIn(b"\r\n", raw, f"{path.name} 混入了 CRLF")
        text = (self.out / "mindmap.md").read_text(encoding="utf-8")
        self.assertIn("胶质瘤", text)  # 中文没有被写坏

    def test_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            render_mindmap(self.bundle, self.out, depth="panels")
        with self.assertRaises(ValueError):
            render_mindmap(self.bundle, self.out, formats=["markdown"])


class TestHostileText(TempDirCase):
    """对抗性用例：mermaid / HTML / XML 三层转义都不许漏。"""

    def setUp(self) -> None:
        super().setUp()
        self.bundle = hostile_bundle()

    def test_mermaid_survives_hostile_text(self):
        for depth in DEPTHS:
            with self.subTest(depth=depth):
                assert_valid_mermaid(
                    self, to_mermaid(build_paper_tree(self.bundle, depth)), depth
                )
                for rec in self.bundle.figures:
                    assert_valid_mermaid(
                        self,
                        to_mermaid(build_figure_tree(rec, depth)),
                        f"{rec.figure_id}/{depth}",
                    )

    def test_mermaid_directive_injection_neutralised(self):
        """`%%{init:...}%%` 若漏出去会改掉整张图的渲染配置。"""
        source = to_mermaid(build_paper_tree(self.bundle, "result"))
        self.assertNotIn("%%", source)
        self.assertNotIn("{init", source)
        self.assertNotIn("::icon", source)
        self.assertNotIn(":::", source)
        self.assertNotIn("root((", source)

    def test_code_fence_cannot_escape_markdown_block(self):
        """``` 会提前闭合 .md 里的围栏——不只导图节点，标题行与图题行同样危险。

        图题直接来自 caption 原文，是**没有**经过 mermaid 转义的一条路径：
        它写在围栏外的 markdown 正文里，得由 `_md_inline` 单独中和。
        """
        self.assertIn("```", self.bundle.figures[0].caption)  # 前提：caption 里真有围栏
        paths = render_mindmap(self.bundle, self.out, depth="result", formats=["mermaid"])
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("```"), 2, f"{path.name} 的围栏数量不对")
            assert_valid_mermaid(self, mermaid_of(text), path.name)

    def test_markdown_body_neutralises_raw_html(self):
        """图题里的裸 HTML 不能原样落进 .md——Obsidian 会当 HTML 渲染。"""
        bundle = hostile_bundle()
        bundle.figures[0].caption = "Fig. 1. <script>alert(1)</script> and `code`."
        render_mindmap(bundle, self.out, formats=["mermaid"])
        text = (self.out / figure_mindmap_relpath("Figure 1")).read_text(encoding="utf-8")
        assert_caption_line(self, text, "&lt;script>alert(1)&lt;/script> and ＇code＇")
        self.assertNotIn("<script>", text)   # 整份文件都不许有裸标签
        self.assertEqual(text.count("```"), 2)

    def test_markdown_preserves_inequalities_and_ampersands(self):
        """图题是拿去原文里 Ctrl+F 的定位坐标，改一个字符就搜不到。

        `p<0.001` / `log2FC > 1` / `Nrf2 & HMOX1` 在生物医学图题里是常态，
        无差别转义 `<` 或 `&` 会毁掉这个用途。只有标签起始的 `<` 才该被中和。
        """
        caption = (
            "Fig. 1. Genes with log2FC > 1 and p<0.05 in Nrf2 & HMOX1 pathway. (A) foo."
        )
        bundle = hostile_bundle()
        bundle.figures[0].caption = caption
        render_mindmap(bundle, self.out, formats=["mermaid"])
        # 只看 markdown 正文里的图题行。同一个文件里的 mermaid 块**应该**含全角
        # `p＜0.05`——两处转义规则本就不同，断言范围盖到整个文件就分不清是谁的输出了
        assert_caption_line(
            self,
            (self.out / figure_mindmap_relpath("Figure 1")).read_text(encoding="utf-8"),
            "Genes with log2FC > 1 and p<0.05 in Nrf2 & HMOX1 pathway",
        )

    def test_existing_entities_not_double_escaped(self):
        """已经是实体的文本不能再被转一层（`&lt;` 不得变成 `&amp;lt;`）。"""
        bundle = hostile_bundle()
        bundle.figures[0].caption = "Fig. 1. Title with &lt;b&gt; and &amp; inside. (A) foo."
        render_mindmap(bundle, self.out, formats=["mermaid"])
        assert_caption_line(
            self,
            (self.out / figure_mindmap_relpath("Figure 1")).read_text(encoding="utf-8"),
            "Title with &lt;b&gt; and &amp; inside",
        )

    def test_newline_in_caption_cannot_escape_blockquote(self):
        """caption 里一个换行就能从 `> **图题**` 引用块里逃出去。"""
        bundle = hostile_bundle()
        bundle.figures[0].caption = "Fig. 1. Title line.\n\n# 伪标题\n- 伪列表"
        render_mindmap(bundle, self.out, formats=["mermaid"])
        text = (self.out / figure_mindmap_relpath("Figure 1")).read_text(encoding="utf-8")
        self.assertNotIn("\n# 伪标题", text)
        self.assertNotIn("\n- 伪列表", text)

    def test_long_text_is_truncated(self):
        source = to_mermaid(build_paper_tree(self.bundle, "result"))
        for line in source.split("\n")[1:]:
            self.assertLessEqual(len(line.strip()), MAX_LINE_CHARS)
        self.assertIn("…", source)  # 240 字的输入确实被截了

    def test_degenerate_panels_and_figures(self):
        """label 为 `-` / 空 groups / 全 not_reported 的图都不能炸。"""
        source = to_mermaid(build_paper_tree(self.bundle, "result"))
        assert_valid_mermaid(self, source, "degenerate")
        self.assertIn("Table 10", source)
        self.assertNotIn("Panel -", source)

    def test_opml_is_well_formed_xml(self):
        for depth in DEPTHS:
            xml = to_opml(build_paper_tree(self.bundle, depth), title=HOSTILE[5])
            root = ET.fromstring(xml)  # 非法 XML 会抛 ParseError
            self.assertEqual(root.tag, "opml")
            self.assertEqual(root.get("version"), "2.0")
            outlines = root.findall(".//outline")
            self.assertEqual(len(outlines), build_paper_tree(self.bundle, depth).count())
            for outline in outlines:
                self.assertTrue((outline.get("text") or "").strip(), "OPML 有空 text 属性")

    def test_opml_keeps_original_characters(self):
        """OPML 用 XML 实体转义，不该沿用 mermaid 那套全角替换。"""
        xml = to_opml(build_paper_tree(load_sample(), "result"))
        texts = [o.get("text", "") for o in ET.fromstring(xml).findall(".//outline")]
        joined = "\n".join(texts)
        self.assertIn("-log10(padj)", joined)  # 半角圆括号原样保留
        self.assertIn("*** p<0.001", joined)   # 解析回来是真正的 < 而非 ＜
        self.assertIn("凋亡率（%）", joined)      # 半角百分号原样保留
        self.assertNotIn("＜", joined)          # 没有沿用 mermaid 的全角替换

    def test_html_is_self_contained(self):
        html = to_markmap_html(build_paper_tree(self.bundle, "result"), title=HOSTILE[6])
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertTrue(html.rstrip().endswith("</html>"))
        for forbidden in ("<script src", "http://", "https://", "@import", "url("):
            self.assertNotIn(forbidden, html, f"HTML 引用了外部资源：{forbidden}")

    def test_html_json_payload_cannot_break_out(self):
        """节点文本里的 `</script>` 不能逃逸出 JSON script 标签。"""
        html = to_markmap_html(build_paper_tree(self.bundle, "result"))
        payload = re.search(
            r'<script type="application/json" id="tree-data">(.*?)</script>', html, re.S
        )
        self.assertIsNotNone(payload, "找不到内嵌数据块")
        raw = payload.group(1)
        for char in "<>&":
            self.assertNotIn(char, raw, f"内嵌 JSON 里有裸 {char}")
        data = json.loads(raw)  # 必须仍是合法 JSON
        self.assertEqual(_count_json(data), build_paper_tree(self.bundle, "result").count())
        # 转义只是传输编码，解析回来的文本仍是原文
        self.assertIn("<script>", json.dumps(data, ensure_ascii=False))

    def test_html_javascript_parses(self):
        """用 node --check 真的解析一遍内嵌 JS，避免语法错误静默上线。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 node，跳过 JS 语法检查")
        html = to_markmap_html(build_paper_tree(self.bundle, "result"))
        scripts = re.findall(r"<script>\n(.*?)\n</script>", html, re.S)
        self.assertEqual(len(scripts), 1, "期望恰好一个行为脚本")
        js = Path(self._tmp.name) / "check.js"
        js.write_text(scripts[0], encoding="utf-8")
        proc = subprocess.run(
            [node, "--check", str(js)], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(proc.returncode, 0, f"内嵌 JS 语法错误：\n{proc.stderr}")


def _count_json(node: dict) -> int:
    return 1 + sum(_count_json(c) for c in node.get("c", []))


class TestCorpus(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        first = load_sample()
        second = load_sample()
        second.paper.title = "Temozolomide resistance in glioma: a second paper"
        second.paper.year = 2024
        self.bundles = [first, second, hostile_bundle()]

    def test_corpus_mindmap_written_and_valid(self):
        path = render_corpus_mindmap(self.bundles, self.out)
        self.assertEqual(path.name, "mindmap.md")
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        assert_valid_mermaid(self, mermaid_of(text), "corpus")
        # 契约只返回 .md，但兄弟格式也要落盘并在正文里指路
        self.assertTrue((self.out / "mindmap.html").is_file())
        self.assertTrue((self.out / "mindmap.opml").is_file())
        self.assertIn("mindmap.html", text)
        ET.fromstring((self.out / "mindmap.opml").read_text(encoding="utf-8"))

    def test_corpus_merges_shared_axes(self):
        """同一个研究问题下应挂着多篇论文的证据——这正是跨文献导图的用处。"""
        source = to_mermaid(build_corpus_tree(self.bundles))
        self.assertIn("P1", source)
        self.assertIn("P2", source)
        self.assertIn("P3", source)
        question = "DMC-GF 是否在体外对胶质瘤细胞具有抗肿瘤活性"
        lines = source.split("\n")
        index = next(i for i, l in enumerate(lines) if question in l)
        children = [l.strip() for l in lines[index + 1 : index + 3]]
        # 证据行可能带 ⚠ 前缀（该图需人工复核），断言引用键而非行首
        self.assertIn("P1 · Figure 1", children[0])
        self.assertIn("P2 · Figure 1", children[1], f"P2 未归并到同一问题下：{children}")

    def test_corpus_handles_empty_and_single(self):
        assert_valid_mermaid(self, to_mermaid(build_corpus_tree([])), "empty")
        path = render_corpus_mindmap([load_sample()], self.out / "one")
        assert_valid_mermaid(self, mermaid_of(path.read_text(encoding="utf-8")), "single")


def fabricated_bundle() -> PaperBundle:
    """复现真实缺陷：模型脑补了 `n` 与 `stats.test`，却谎称 `inferred: []` + `confidence: high`。

    这是最危险的一类记录——**自报完全干净**，只有 merge 阶段的哨兵能抓住。
    警告文本格式照抄端到端跑出来的真实产物。
    """
    honest = FigureRecord(
        figure_id="Figure 1",
        question="诚实抽取的对照图",
        panels=[
            Panel(
                label="A",
                experiment="CCK-8 检测细胞活力",
                groups=[Group(name="0 μM", role="control", result="基线")],
                stats=Stats(),  # 原文没写就留 not_reported
                evidence=Evidence(from_caption=["experiment"]),
            )
        ],
        figure_conclusion="诚实结论",
        confidence=Confidence.HIGH,
    )
    fabricated = FigureRecord(
        figure_id="Figure 4",
        question="DMC-GF 是否经 Keap1/Nrf2 上调 HMOX1？",
        panels=[
            Panel(
                label="A",
                experiment="Western blot 检测 Nrf2 与 HMOX1 蛋白表达",
                groups=[
                    Group(
                        name="DMC-GF",
                        role="treatment",
                        n="3",  # ← 无依据
                        result="HMOX1 上调约 3 倍",
                    )
                ],
                stats=Stats(test="t-test", error_bar="SD"),  # ← 原文没说
                evidence=Evidence(inferred=[]),  # ← 谎称零推断
            )
        ],
        figure_conclusion="DMC-GF 上调 HMOX1",
        confidence=Confidence.HIGH,  # ← 谎称高置信
    )
    return PaperBundle(
        paper=PaperMeta(title="脑补检测用例", year=2026),
        figures=[honest, fabricated],
        warnings=[
            "[复核] figure_04.json · Figure 4 panels[0](A): 填了 n='3' 却没在 evidence "
            "里溯源——n 是最高危字段，请确认不是脑补，否则改回 not_reported",
            "[复核] figure_04.json · Figure 4 panels[0](A): 填了 stats.test='t-test' 却没在 "
            "evidence 里溯源——看见星号不等于知道用了什么检验",
        ],
    )


class TestFigureTitle(TempDirCase):
    """图题抽取。读文献时图题是定位坐标，与 `question`（我们的二次归纳）作用不同。"""

    def test_extracts_title_from_real_caption_shapes(self):
        cases = [
            # (caption, 期望的图题)
            ("Fig. 1. DMC-GF exhibits pronounced antitumour activity in vitro. "
             "(A) CCK-8 assay.",
             "DMC-GF exhibits pronounced antitumour activity in vitro"),
            # 句号前是数字，但那是句末不是小数点——早期实现在这里整段不断句
            ("Fig. 4. DMC-GF enhances HMOX1 expression via Keap1/Nrf2. "
             "All images were acquired identically.",
             "DMC-GF enhances HMOX1 expression via Keap1/Nrf2"),
            ("Figure 2: Ablation study on WMT14. We report BLEU.",
             "Ablation study on WMT14"),
            ("FIGURE 1 | Overview of the proposed framework.",
             "Overview of the proposed framework"),          # Frontiers 竖线式
            ("Table IV. Comparison with prior work.", "Comparison with prior work"),
            ("图 5 DMC-GF 对肿瘤生长的影响。（A）体重变化。", "DMC-GF 对肿瘤生长的影响"),
            ("Fig. 9. No trailing period here", "No trailing period here"),
        ]
        for caption, expected in cases:
            with self.subTest(caption=caption[:40]):
                rec = FigureRecord(figure_id="Figure 1", caption=caption)
                self.assertEqual(figure_title(rec), expected)

    def test_does_not_split_on_decimals_or_abbreviations(self):
        traps = [
            ("Fig. 7. IC50 was 5.148 uM at 48 h. Next panel.",
             "IC50 was 5.148 uM at 48 h"),                    # 小数点
            ("Fig. 8. Results for e.g. U251 and i.p. dosing. Second.",
             "Results for e.g. U251 and i.p. dosing"),        # 缩写
            ("Fig 3a Dose response of A. baumannii to colistin. (a) growth.",
             "Dose response of A. baumannii to colistin"),    # 属名缩写
        ]
        for caption, expected in traps:
            with self.subTest(caption=caption[:40]):
                self.assertEqual(
                    figure_title(FigureRecord(figure_id="Figure 1", caption=caption)),
                    expected,
                )

    def test_does_not_strip_prefix_without_figure_number(self):
        """`Figure quality assessment…` 是正常句子，不能被当成图号前缀削掉。"""
        rec = FigureRecord(
            figure_id="Figure 1", caption="Figure quality assessment across datasets. Next."
        )
        self.assertEqual(figure_title(rec), "Figure quality assessment across datasets")

    def test_missing_caption_yields_empty(self):
        for caption in ("", "   ", "Fig. 10."):
            self.assertEqual(figure_title(FigureRecord(figure_id="F", caption=caption)), "")

    def test_root_uses_title_and_demotes_question(self):
        rec = load_sample().figures[0]
        source = to_mermaid(build_figure_tree(rec, "panel", []))
        lines = source.split("\n")
        self.assertIn("DMC-GF exhibits pronounced antitumour activity", lines[1])
        self.assertNotIn("是否在体外", lines[1])          # question 不再占根节点
        self.assertTrue(any(l.strip().startswith("❓ ") for l in lines))

    def test_falls_back_to_question_without_caption(self):
        rec = load_sample().figures[0]
        rec.caption = ""
        source = to_mermaid(build_figure_tree(rec, "panel", []))
        lines = source.split("\n")
        self.assertIn("是否在体外", lines[1])
        # 没有图题时不该再挂一个重复的 ❓ 子节点
        self.assertFalse(any(l.strip().startswith("❓ ") for l in lines))

    def test_paper_tree_still_keyed_on_question(self):
        """证据链导图讲的是逻辑链，图节点仍用 question，不换成图题。"""
        source = to_mermaid(build_paper_tree(load_sample(), "figure"))
        self.assertIn("是否在体外", source)
        self.assertNotIn("exhibits pronounced antitumour", source)

    def test_fragment_markdown_carries_caption_title(self):
        render_mindmap(load_sample(), self.out, formats=["mermaid"])
        text = (self.out / figure_mindmap_relpath("Figure 1")).read_text(encoding="utf-8")
        self.assertIn("**图题**", text)
        self.assertIn("DMC-GF exhibits pronounced antitumour activity in vitro", text)

    def test_per_figure_html_written_for_markmap(self):
        paths = render_mindmap(load_sample(), self.out, formats=["markmap"])
        names = {p.name for p in paths}
        self.assertIn("figure_01.html", names)
        html = (self.out / MINDMAP_DIR / "figure_01.html").read_text(encoding="utf-8")
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        for forbidden in ("<script src", "http://", "https://"):
            self.assertNotIn(forbidden, html)
        # 只要 mermaid 时不应产出 html
        plain = render_mindmap(load_sample(), self.out / "md", formats=["mermaid"])
        self.assertFalse([p for p in plain if p.suffix == ".html"])

    def test_design_levels_only_at_result_depth(self):
        rec = load_sample().figures[0]
        self.assertNotIn("设计水平", to_mermaid(build_figure_tree(rec, "group", [])))
        deep = to_mermaid(build_figure_tree(rec, "result", []))
        self.assertIn("设计水平 ", deep)
        self.assertIn("12 h、24 h", deep)


def multi_panel_sentinel_bundle() -> PaperBundle:
    """多 panel 图 + 哨兵只命中其中一个 panel —— `ReviewReason.panel` 存在的唯一理由。

    真实 e2e 数据里触发不到（唯一被哨兵抓的图恰好只有 1 个 panel），
    但这个组合迟早出现：图级 ⚠ 只能说「这张图有问题」，面对 A–E 五个 panel
    读者仍然不知道该核哪个。另外挂一条 `panel=None` 的图级理由，
    验证它**不会**被误打到任何 panel 上。
    """
    rec = FigureRecord(
        figure_id="Figure 3",
        question="五联图，只有一个 panel 有问题",
        confidence=Confidence.HIGH,
        panels=[
            Panel(
                label=label,
                experiment=f"第 {label} 组实验",
                groups=[Group(name="对照", role="control", result="基线")],
                evidence=Evidence(from_caption=["experiment"]),
            )
            for label in "ABCDE"
        ],
        figure_conclusion="五个 panel 的合并结论",
    )
    return PaperBundle(
        paper=PaperMeta(title="多 panel 哨兵用例"),
        figures=[rec],
        warnings=[
            "[复核] figure_03.json · Figure 3 panels[2](C): 填了 n='3' 却没在 "
            "evidence 里溯源——n 是最高危字段",
            "[复核] figure_03.json · Figure 3: evidence 五个桶全为空，违反逐字段溯源铁律",
        ],
    )


class TestPanelAttribution(TempDirCase):
    """`ReviewReason.panel` 落到正确的 panel 节点上。

    契约把 panel 归属结构化之前，哨兵理由的 `panels[2](C)` 会在
    `w.split(":", 1)` 那步被切掉，导图只能标到图级——五联图里读者仍需逐个排查。
    """

    def setUp(self) -> None:
        super().setUp()
        self.bundle = multi_panel_sentinel_bundle()
        self.rec = self.bundle.figures[0]
        self.reasons = review_reasons(self.bundle)["Figure 3"]

    def test_contract_provides_panel_attribution(self):
        kinds = {(r.kind, r.panel) for r in self.reasons}
        self.assertIn(("sentinel", "C"), kinds)
        self.assertIn(("sentinel", None), kinds, "图级理由应保持 panel=None")

    def test_mark_lands_only_on_named_panel(self):
        for depth in ("panel", "group", "result"):
            with self.subTest(depth=depth):
                source = to_mermaid(build_figure_tree(self.rec, depth, self.reasons))
                assert_valid_mermaid(self, source, depth)
                self.assertIn("⚠ Panel C", source)
                for label in "ABDE":
                    self.assertIn(f"Panel {label}", source)
                    self.assertNotIn(f"⚠ Panel {label}", source, f"Panel {label} 被误标")

    def test_paper_tree_marks_only_named_panel(self):
        source = to_mermaid(build_paper_tree(self.bundle, "panel"))
        assert_valid_mermaid(self, source, "paper/panel")
        self.assertIn("⚠ Figure 3", source)
        self.assertIn("⚠ Panel C", source)
        self.assertNotIn("⚠ Panel A", source)

    def test_figure_level_reason_marks_no_panel(self):
        """`panel=None` 的理由只该抬高图节点，不该落到任何 panel。"""
        only_figure_level = [r for r in self.reasons if r.panel is None]
        self.assertTrue(only_figure_level)
        source = to_mermaid(build_figure_tree(self.rec, "panel", only_figure_level))
        self.assertIn("⚠ Figure 3", source)
        for label in "ABCDE":
            self.assertNotIn(f"⚠ Panel {label}", source)

    def test_pending_lists_panel_and_kind(self):
        """待确认行要自带归属，否则还得回头对照 panel 节点。"""
        source = to_mermaid(build_figure_tree(self.rec, "group", self.reasons))
        self.assertIn("哨兵 · Panel C · ", source)
        self.assertIn("哨兵 · evidence 五个桶全为空", source)  # 图级理由不写 Panel

    def test_reasons_sorted_sentinel_first(self):
        """哨兵是独立校验，排在自报的推断/低置信之前。"""
        rec = self.rec.model_copy(deep=True)
        rec.panels[0].evidence.inferred = ["groups[].n"]
        rec.confidence = Confidence.LOW
        bundle = self.bundle.model_copy(deep=True)
        bundle.figures[0] = rec
        source = to_mermaid(build_figure_tree(rec, "group", review_reasons(bundle)["Figure 3"]))
        lines = [l.strip() for l in source.split("\n")]
        start = lines.index("⚠ 待确认")
        order = [l.split(" · ")[0] for l in lines[start + 1 : start + 5]]
        self.assertEqual(order, ["哨兵", "哨兵", "推断", "低置信"], f"排序不对：{order}")

    def test_written_files_keep_panel_precision(self):
        render_mindmap(self.bundle, self.out, depth="group", formats=list(FORMATS))
        fragment = (self.out / figure_mindmap_relpath("Figure 3")).read_text(encoding="utf-8")
        self.assertIn("⚠ Panel C", fragment)
        self.assertNotIn("⚠ Panel A", fragment)
        main = (self.out / "mindmap.md").read_text(encoding="utf-8")
        self.assertIn("⚠ Panel C", main)
        self.assertNotIn("⚠ Panel A", main)


class TestSentinelReview(TempDirCase):
    """回归：自报干净但哨兵存疑的图，必须照样带 ⚠。

    曾经的缺陷是图级 ⚠ 只看 `needs_review(panel)`（即 panel 自报的
    `evidence.inferred`）与 `confidence`。会脑补的模型恰恰最可能谎报 `inferred: []`，
    于是**造假记录在导图里显得比诚实抽取的还干净**——最坏的失败模式。
    现在图级一律走 `models.review_reasons()`，它并入了 merge 哨兵的 `[复核]` 警告。
    """

    def setUp(self) -> None:
        super().setUp()
        self.bundle = fabricated_bundle()
        self.fake = self.bundle.figures[1]

    def test_precondition_self_report_looks_clean(self):
        """先确认这条记录确实骗过了所有自报信号，否则本用例就白测了。"""
        self.assertEqual(self.fake.confidence, Confidence.HIGH)
        self.assertFalse(any(needs_review(p) for p in self.fake.panels))
        self.assertIn("Figure 4", review_reasons(self.bundle))
        self.assertNotIn("Figure 1", review_reasons(self.bundle))

    def test_fabricated_figure_marked_at_every_depth(self):
        for depth in DEPTHS:
            with self.subTest(depth=depth):
                source = to_mermaid(build_paper_tree(self.bundle, depth))
                assert_valid_mermaid(self, source, depth)
                self.assertIn("⚠ Figure 4", source, "哨兵存疑的图没有被标 ⚠")
                self.assertNotIn("⚠ Figure 1", source, "诚实抽取的图被误标 ⚠")

    def test_figure_fragment_shows_sentinel_reason(self):
        """片段里不光要标 ⚠，还得说清楚要核什么——⚠ 本身不可操作。"""
        reasons = review_reasons(self.bundle)["Figure 4"]
        source = to_mermaid(build_figure_tree(self.fake, "group", reasons))
        assert_valid_mermaid(self, source, "fabricated fragment")
        self.assertIn("⚠ Figure 4", source)
        self.assertIn("⚠ Panel A", source)  # 哨兵点名了 panel A
        self.assertIn("⚠ 待确认", source)
        # 断言 kind 标签（本模块自己的展示层），不断言契约的理由文案——
        # 文案是纯展示物，正是 ReviewReason.kind 要消除的耦合
        self.assertIn("哨兵 · Panel A · ", source)
        self.assertIn("n='3'", source)

    def test_written_files_carry_the_mark(self):
        """端到端：⚠ 必须真的落到磁盘上的 .md，而不只是内存里的树。"""
        render_mindmap(self.bundle, self.out, depth="group", formats=list(FORMATS))
        main = (self.out / "mindmap.md").read_text(encoding="utf-8")
        self.assertIn("⚠ Figure 4", main)
        self.assertNotIn("⚠ Figure 1", main)

        fragment = (self.out / figure_mindmap_relpath("Figure 4")).read_text(encoding="utf-8")
        self.assertIn("⚠ Figure 4", fragment)
        self.assertIn("哨兵 · Panel A · ", fragment)

        clean = (self.out / figure_mindmap_relpath("Figure 1")).read_text(encoding="utf-8")
        self.assertNotIn("⚠", mermaid_of(clean))

        # HTML 与 OPML 走的是同一棵树，标记不能只在 mermaid 上有
        self.assertIn("⚠ Figure 4", (self.out / "mindmap.html").read_text(encoding="utf-8"))
        self.assertIn("⚠ Figure 4", (self.out / "mindmap.opml").read_text(encoding="utf-8"))

    def test_legend_explains_sentinel(self):
        render_mindmap(self.bundle, self.out, formats=["mermaid"])
        legend = (self.out / "mindmap.md").read_text(encoding="utf-8")
        self.assertIn("校验哨兵", legend)
        self.assertIn("inferred", legend)

    def test_corpus_marks_flagged_figures(self):
        source = to_mermaid(build_corpus_tree([load_sample(), self.bundle]))
        assert_valid_mermaid(self, source, "corpus sentinel")
        self.assertIn("⚠ P2 · Figure 4", source)
        self.assertIn("⚠ 1 张图需人工复核", source)
        self.assertNotIn("⚠ P2 · Figure 1", source)

        path = render_corpus_mindmap([self.bundle], self.out / "corpus")
        self.assertIn("⚠ P1 · Figure 4", path.read_text(encoding="utf-8"))

    def test_render_figure_mindmap_accepts_reasons(self):
        """render_notes 的对接入口也要能传理由，否则笔记里的片段会漏标。"""
        reasons = review_reasons(self.bundle)["Figure 4"]
        with_reasons = render_figure_mindmap(self.fake, "group", reasons=reasons)
        self.assertIn("⚠ Figure 4", with_reasons)
        self.assertIn("哨兵 · Panel A · ", with_reasons)
        # 不传时退化成只看自报信息——这正是缺陷的形状，明确记录下来
        self.assertNotIn("⚠", mermaid_of(render_figure_mindmap(self.fake, "group")))

    def test_empty_reasons_means_verified_clean(self):
        """显式传空列表 = 已核对过，与「没传」要能区分开。"""
        low = load_sample().figures[0]
        low.confidence = Confidence.LOW
        self.assertIn("⚠", to_mermaid(build_figure_tree(low, "group")))
        self.assertNotIn("⚠ Figure 1", to_mermaid(build_figure_tree(low, "group", [])))


class TestNaming(unittest.TestCase):
    """文件名约定是与 render_notes 的接口，改动会破坏引用。"""

    def test_figure_slug(self):
        cases = {
            "Figure 1": "figure_01",
            "Figure 10": "figure_10",
            "Fig. 2": "fig_02",
            "Table 3": "table_03",
            "Figure 4b": "figure_04b",
            "图 5": "图_05",
            "Supplementary Figure 1": "supplementary_figure_1",
        }
        for raw, expected in cases.items():
            self.assertEqual(figure_slug(raw), expected, raw)

    def test_slug_never_empty(self):
        for raw in ("", "   ", "???", "\x00"):
            self.assertTrue(figure_slug(raw))

    def test_relpath_matches_written_files(self):
        self.assertEqual(figure_mindmap_relpath("Figure 1"), f"{MINDMAP_DIR}/figure_01.md")


class TestEmptyBundle(TempDirCase):
    def test_bundle_without_figures(self):
        bundle = PaperBundle(paper=PaperMeta(title="空壳论文"), figures=[])
        paths = render_mindmap(bundle, self.out, formats=list(FORMATS))
        self.assertTrue(paths)
        assert_valid_mermaid(
            self, mermaid_of((self.out / "mindmap.md").read_text(encoding="utf-8")), "empty"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
