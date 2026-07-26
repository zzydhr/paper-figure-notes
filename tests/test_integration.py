"""端到端集成测试 —— 并行开发的接缝检查。

分两段跑（L3 由 agent 完成，无法自动化）：
    段一  contract  各模块是否按约定暴露了入口签名        ← 并行开发最易翻车处
    段二  prep      L1+L2 在两篇真实论文上跑通
    段三  render    L4 用 sample_bundle.json 跑通

用法::
    python tests/test_integration.py            # 全跑
    python tests/test_integration.py contract   # 只查契约
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "paper_figure_notes.py"
SAMPLE = ROOT / "tests" / "fixtures" / "sample_bundle.json"

FIXTURES = Path(
    r"C:\Users\zzy\AppData\Local\Temp\claude\D--Tools"
    r"\6ae6199e-4d49-499a-996d-459b0e8a77cc\scratchpad\fixtures"
)

sys.path.insert(0, str(SCRIPTS))

PASS, FAIL = "  [PASS]", "  [FAIL]"
_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    if cond:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))
        _failures.append(label)
    return cond


# ── 段一：契约一致性 ────────────────────────────────────────────────────────

#: (模块, 函数, 必须存在的参数名)
CONTRACT = [
    ("pfn.parse", "parse_pdf", ["pdf", "out_dir"]),
    ("pfn.context", "build_context_packs", ["parsed", "out_dir"]),
    ("pfn.extract", "build_extraction_tasks", ["out_dir"]),
    ("pfn.extract", "validate_record", []),
    ("pfn.extract", "merge_records", ["out_dir"]),
    ("pfn.render_notes", "render_notes", ["bundle", "out_dir"]),
    ("pfn.render_table", "render_tables", ["bundle", "out_dir"]),
    ("pfn.render_table", "render_corpus_table", []),
    ("pfn.render_mindmap", "render_mindmap", ["bundle", "out_dir"]),
    ("pfn.render_mindmap", "render_corpus_mindmap", []),
]


def test_contract() -> None:
    print("\n=== 段一：契约一致性 ===")
    import importlib

    for mod_name, fn_name, params in CONTRACT:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:  # noqa: BLE001
            check(False, f"import {mod_name}", f"{type(e).__name__}: {e}")
            continue
        fn = getattr(mod, fn_name, None)
        if not check(callable(fn), f"{mod_name}.{fn_name} 存在且可调用"):
            continue
        sig = inspect.signature(fn)
        missing = [p for p in params if p not in sig.parameters]
        check(
            not missing,
            f"{mod_name}.{fn_name} 参数齐全",
            f"缺少 {missing}，实际为 {list(sig.parameters)}",
        )


# ── 段二：prep（L1 + L2）────────────────────────────────────────────────────

#: (文件名, 期望图数, 期望表数, 说明)
PREP_CASES = [
    ("acl_shifcon.pdf", 11, 19, "ACL 双栏 LaTeX，矢量图为主"),
    ("scirep_dmcgf.pdf", 5, 0, "Scientific Reports 单栏，含跨页图"),
]


def test_prep(workdir: Path) -> None:
    print("\n=== 段二：prep（L1+L2）===")
    for name, want_fig, want_tab, note in PREP_CASES:
        pdf = FIXTURES / name
        if not pdf.is_file():
            check(False, f"{name} 存在", f"找不到 {pdf}")
            continue
        out = workdir / f"out_{pdf.stem}"
        print(f"\n-- {name}（{note}）")
        r = subprocess.run(
            [sys.executable, str(CLI), "prep", str(pdf), "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if not check(r.returncode == 0, f"{name} prep 退出码为 0",
                     (r.stderr or "")[-400:]):
            continue

        pj = out / "parsed.json"
        if not check(pj.is_file(), f"{name} 产出 parsed.json"):
            continue
        data = json.loads(pj.read_text(encoding="utf-8"))
        assets = data.get("assets", [])
        figs = [a for a in assets if a["kind"] == "figure"]
        tabs = [a for a in assets if a["kind"] == "table"]
        check(len(figs) == want_fig, f"{name} 检出 {want_fig} 张图",
              f"实得 {len(figs)}")
        if want_tab:
            check(len(tabs) == want_tab, f"{name} 检出 {want_tab} 张表",
                  f"实得 {len(tabs)}")

        # 切图必须真实落盘且非空
        missing = [
            img for a in assets for img in a["images"]
            if not (out / img).is_file() or (out / img).stat().st_size < 1024
        ]
        check(not missing, f"{name} 切图全部落盘且非空", f"缺失/过小：{missing[:4]}")

        # 跨页图合并（仅 SciRep 有）
        if "scirep" in name:
            multi = [a["figure_id"] for a in assets if len(a["pages"]) > 1]
            check(len(multi) >= 2, "SciRep 跨页图已合并（Fig 2 / Fig 3）",
                  f"实得 {multi}")

        low = [a["figure_id"] for a in assets
               if a["bbox_confidence"] != "high"]
        print(f"     切图置信度非 high：{low if low else '无'}")

        check((out / "context.json").is_file(), f"{name} 产出 context.json")
        packs = list((out / "context").glob("*.md")) if (out / "context").is_dir() else []
        check(len(packs) == len(assets), f"{name} context 数与 asset 数一致",
              f"{len(packs)} vs {len(assets)}")


# ── 段三：render（L4）───────────────────────────────────────────────────────


def test_render(workdir: Path) -> None:
    print("\n=== 段三：render（L4）===")
    out = workdir / "out_render"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(SAMPLE, out / "figures.json")

    r = subprocess.run(
        [sys.executable, str(CLI), "render", "--out", str(out),
         "--mindmap-format", "mermaid", "markmap", "opml"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if not check(r.returncode == 0, "render 退出码为 0", (r.stderr or "")[-400:]):
        return

    produced = {p.name for p in out.rglob("*") if p.is_file()}
    for want in ("figure-notes.md", "mindmap.md", "figure-tables.md", "index.html"):
        check(want in produced, f"产出 {want}", f"实得 {sorted(produced)}")
    check(any(n.endswith(".xlsx") for n in produced), "产出 xlsx 长表")

    # 单页必须自包含：数据已内联，且不得引用任何外部主机
    page = out / "index.html"
    if page.is_file():
        html = page.read_text(encoding="utf-8")
        check("__DATA__" not in html, "单页数据已注入（无占位符残留）")
        check('id="D"' in html and '"figs"' in html, "单页内联了图数据")
        # 只禁**会发起加载**的外部引用；正文里的 <a href> 超链接（如 DOI）无碍离线
        ext = re.findall(r'src\s*=\s*"(?:https?:)?//[^"]+', html)
        ext += re.findall(r'<link\b[^>]*href\s*=\s*"(?:https?:)?//[^"]+', html)
        ext += re.findall(r'url\(\s*[\'"]?(?:https?:)?//[^)]+', html)
        ext += re.findall(r'@import\s+[\'"](?:https?:)?//[^\'"]+', html)
        check(not ext, "单页无外部资源引用（离线可用）", f"发现 {ext[:3]}")

    # 四列速览表：列数固定为 4，且分组行与结果行一一对齐
    tbl = out / "figure-tables.md"
    if tbl.is_file():
        text = tbl.read_text(encoding="utf-8")
        check("| 小图 | 实验方法 | 分组 | 结果 |" in text, "速览表表头为四列")
        widths = {
            len(ln.split("|")) - 2
            for ln in text.splitlines()
            if ln.startswith("|") and ln.rstrip().endswith("|")
        }
        check(widths == {4}, "速览表每行都是 4 列", f"实得列宽 {sorted(widths)}")
        check("not_reported" not in text, "速览表未泄漏 not_reported 原始值")

    notes = out / "figure-notes.md"
    if notes.is_file():
        text = notes.read_text(encoding="utf-8")
        check("not_reported" not in text,
              "笔记未泄漏 not_reported 原始值",
              "应渲染为「未说明」或省略")
        check("Figure 1" in text and "Figure 6" in text,
              "笔记覆盖全部 3 张图（含 schematic）")

    mm = out / "mindmap.md"
    if mm.is_file():
        text = mm.read_text(encoding="utf-8")
        check("mindmap" in text, "导图含 mermaid mindmap 块")
        # mermaid mindmap 里裸括号会破坏语法
        in_block = False
        bad: list[str] = []
        for line in text.splitlines():
            if line.strip().startswith("```"):
                in_block = not in_block
                continue
            if in_block and line.strip() and not line.strip().startswith("mindmap"):
                if "(" in line or ")" in line:
                    bad.append(line.strip()[:60])
        check(not bad, "mermaid 节点无未转义括号", f"可疑行：{bad[:3]}")


# ── main ────────────────────────────────────────────────────────────────────


def main() -> int:
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    tmp = Path(tempfile.mkdtemp(prefix="pfn_it_"))
    print(f"工作目录：{tmp}")
    try:
        if stage in ("all", "contract"):
            test_contract()
        if stage in ("all", "prep"):
            test_prep(tmp)
        if stage in ("all", "render"):
            test_render(tmp)
    finally:
        print(f"\n（产物保留在 {tmp}，可人工查看）")

    print("\n" + "=" * 60)
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
