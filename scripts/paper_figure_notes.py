#!/usr/bin/env python
"""paper-figure-notes CLI —— 文献 figure 实验设计提取。

L3（读图抽取）由 Claude Code agent 完成，因此流水线拆成两段：

    1. prep    PDF → 切图 + 上下文        （本 CLI，确定性）
    2. <agent> 读切图 + 上下文 → figures.json
    3. render  figures.json → 笔记/导图/表格（本 CLI，确定性）

用法::

    python paper_figure_notes.py prep   "paper.pdf" [--out DIR] [--dpi 200]
    python paper_figure_notes.py render --out DIR [--mindmap-depth group]
    python paper_figure_notes.py corpus --dirs DIR1 DIR2 ... --out CORPUS_DIR
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许直接 `python paper_figure_notes.py` 运行而无需安装
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pfn.models import PaperBundle, ParsedPaper  # noqa: E402

DEFAULT_DPI = 200
MINDMAP_DEPTHS = ("figure", "panel", "group", "result")
MINDMAP_FORMATS = ("mermaid", "markmap", "opml")


def default_out_dir(pdf: Path) -> Path:
    """默认输出到 PDF 同级的 notes_<论文名>/，与 video-to-notes 约定一致。"""
    return pdf.parent / f"notes_{pdf.stem}"


# ── prep：L1 + L2 ───────────────────────────────────────────────────────────


def cmd_prep(args: argparse.Namespace) -> int:
    from pfn.context import build_context_packs
    from pfn.parse import parse_pdf

    pdf = Path(args.pdf).resolve()
    if not pdf.is_file():
        print(f"[error] PDF 不存在: {pdf}", file=sys.stderr)
        return 2

    out = Path(args.out).resolve() if args.out else default_out_dir(pdf)
    out.mkdir(parents=True, exist_ok=True)

    pages = _parse_pages(args.pages)

    print(f"[L1] 解析 {pdf.name} → {out}")
    parsed = parse_pdf(pdf, out, dpi=args.dpi, pages=pages)

    if not parsed.has_text_layer:
        print(
            "[error] 该 PDF 没有文本层（疑似扫描版），无法提取图题与正文。\n"
            "        请先做 OCR，或换一个可选中文字的版本。",
            file=sys.stderr,
        )
        return 3

    _write_json(out / "parsed.json", parsed.model_dump(mode="json"))
    n_fig = sum(1 for a in parsed.assets if a.kind == "figure")
    n_tab = sum(1 for a in parsed.assets if a.kind == "table")
    print(f"[L1] 完成：{n_fig} 图 / {n_tab} 表，切图 {out / 'figures'}")
    for a in parsed.assets:
        if a.bbox_confidence.value != "high":
            print(f"      ⚠ {a.figure_id} 切图置信度 {a.bbox_confidence.value}（{a.layout_route.value}）")

    print("[L2] 装配每图上下文…")
    packs = build_context_packs(parsed, out)
    _write_json(out / "context.json", [p.model_dump(mode="json") for p in packs])
    print(f"[L2] 完成：{len(packs)} 个 context pack → {out / 'context'}")

    print(
        f"\n下一步（agent）：逐图读 figures/*.png + context/*.md，"
        f"按 schemas/figure.schema.json 写出 {out / 'figures.json'}，"
        f"然后跑 `render`。"
    )
    return 0


# ── render：L4 ──────────────────────────────────────────────────────────────


def cmd_render(args: argparse.Namespace) -> int:
    from pfn.render_mindmap import render_mindmap
    from pfn.render_notes import render_figure_tables, render_notes
    from pfn.render_page import render_page
    from pfn.render_table import render_tables

    out = Path(args.out).resolve()
    figures_json = out / "figures.json"
    if not figures_json.is_file():
        print(
            f"[error] 找不到 {figures_json}。\n"
            f"        请先跑 prep，再由 agent 完成抽取写出该文件。",
            file=sys.stderr,
        )
        return 2

    bundle = PaperBundle.model_validate_json(
        figures_json.read_text(encoding="utf-8")
    )

    want = set(args.outputs)
    if "all" in want:
        want = {"page", "notes", "mindmap", "table"}

    written: list[Path] = []
    if "notes" in want:
        written.append(render_notes(bundle, out))
        # 四列速览表的独立版本。render_notes 内部也会写它，这里显式再调一次，
        # 是为了让产物清单如实列出它，而不是依赖上一行的副作用。
        written.append(render_figure_tables(bundle, out))
    if "mindmap" in want:
        written.extend(
            render_mindmap(bundle, out, depth=args.mindmap_depth,
                           formats=args.mindmap_format)
        )
    if "table" in want:
        written.extend(render_tables(bundle, out))
    page = render_page(bundle, out) if "page" in want else None  # 缺模板时返回 None
    if page is not None:
        written.append(page)

    print(f"[L4] 完成，共 {len(written)} 个产物：")
    if page is not None:
        print(f"      ★ 先看这个：{page}（浏览器打开，离线可用）")
    for p in written:
        print(f"      {p}")
    return 0


# ── corpus：跨文献汇总 ──────────────────────────────────────────────────────


def cmd_corpus(args: argparse.Namespace) -> int:
    from pfn.render_mindmap import render_corpus_mindmap
    from pfn.render_table import render_corpus_table

    bundles: list[PaperBundle] = []
    for d in args.dirs:
        fj = Path(d).resolve() / "figures.json"
        if not fj.is_file():
            print(f"[warn] 跳过（无 figures.json）：{d}", file=sys.stderr)
            continue
        bundles.append(PaperBundle.model_validate_json(fj.read_text(encoding="utf-8")))

    if not bundles:
        print("[error] 没有可汇总的论文。", file=sys.stderr)
        return 2

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    written = [render_corpus_table(bundles, out), render_corpus_mindmap(bundles, out)]
    print(f"[corpus] 汇总 {len(bundles)} 篇 → {out}")
    for p in written:
        print(f"      {p}")
    return 0


# ── helpers ─────────────────────────────────────────────────────────────────


def _write_json(path: Path, data: object) -> None:
    """统一 UTF-8 写盘。Windows 下 ensure_ascii=False 必须配 encoding='utf-8'。"""
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _parse_pages(spec: str | None) -> list[int] | None:
    """`--pages 1-5,8` → [1,2,3,4,5,8]（1-based）。None 表示全部。"""
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paper-figure-notes",
        description="文献 PDF → 每张 figure 的实验设计（分组 / 结果）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("prep", help="L1+L2：切图 + 装配上下文")
    sp.add_argument("pdf", help="论文 PDF 路径")
    sp.add_argument("--out", help="输出目录，默认 <pdf目录>/notes_<论文名>/")
    sp.add_argument("--dpi", type=int, default=DEFAULT_DPI, help=f"切图 DPI（默认 {DEFAULT_DPI}）")
    sp.add_argument("--pages", help="只处理指定页，如 1-5,8")
    sp.set_defaults(func=cmd_prep)

    sr = sub.add_parser("render", help="L4：笔记 / 思维导图 / Excel")
    sr.add_argument("--out", required=True, help="prep 用的同一个输出目录")
    sr.add_argument(
        "--mindmap-depth", choices=MINDMAP_DEPTHS, default="group",
        help="导图展开到哪一层（默认 group；再深容易糊）",
    )
    sr.add_argument(
        "--mindmap-format", nargs="+", choices=MINDMAP_FORMATS,
        default=["mermaid", "markmap"], help="导图输出格式（可多选）",
    )
    sr.add_argument(
        "--outputs", nargs="+", choices=("all", "page", "notes", "mindmap", "table"),
        default=["all"],
        help="只生成哪些产物。`page` 是自包含单页 index.html——若你只看它，"
             "用 `--outputs page` 可跳过其余（省约 1.5 秒，不省 token：渲染本来就零 token）",
    )
    sr.set_defaults(func=cmd_render)

    sc = sub.add_parser("corpus", help="跨文献汇总")
    sc.add_argument("--dirs", nargs="+", required=True, help="多个论文输出目录")
    sc.add_argument("--out", required=True, help="汇总输出目录")
    sc.set_defaults(func=cmd_corpus)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
