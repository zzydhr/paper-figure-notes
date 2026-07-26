"""L4 · 单文件可视化页面。

产出 `index.html`——自包含、离线可用、双击就能看。三个视图：
逐图精读（左栏图列表 + 右侧完整详情）、分组检索（全部实验组可筛）、待复核。

设计取舍：
- **切图内嵌为缩略图**。原始切图动辄 1~2 MB，18 张就 20 MB 打不开；
  压到 720px/q48 后整页约 2 MB，够看清子图布局与坐标轴，读具体数值仍回原图。
- **`not_reported` 一律渲染成灰斜体「未说明」**，且保留括号里的理由——
  一眼能区分「原文没交代」和「提取遗漏」，这是本工具可信度的外在表现。
- **模板与数据分离**（`assets/page.html`），改样式不必动 Python。
"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any, Optional

from pfn.models import PaperBundle, is_reported, review_reasons

ASSETS = Path(__file__).resolve().parent / "assets"
PAGE_TEMPLATE = ASSETS / "page.html"
PAGE_FILENAME = "index.html"

#: 缩略图上限。再大页面就超过浏览器舒适区间，再小坐标轴刻度糊掉。
THUMB_WIDTH = 720
THUMB_QUALITY = 48

NOT_REPORTED_ZH = "未说明"
_NR_REASON_RE = re.compile(r"^not_reported\s*[（(](.+)[）)]\s*$", re.I)
#: 图题里的图号前缀，展示标题时去掉（`Fig. 3 | Splenic …` → `Splenic …`）
_LABEL_RE = re.compile(r"^(?:Extended Data\s+)?Fig(?:ure)?\.?\s*\d+\s*[|.:]\s*", re.I)

#: Extended Data 图题里写明的归属，如 `…, related to Fig. 6`。
#: 有了它才能画出「正文主线 + 附图支线」的全文流程，而不是 18 个并列节点。
_RELATED_RE = re.compile(r"related\s+to\s+(?:Extended\s+Data\s+)?Fig(?:ure)?\.?\s*(\d+)", re.I)

#: 溯源里的章节引用，形如 `§2.1.1.5.1.1¶2`
_SECTION_REF_RE = re.compile(r"§([\d.]+)(¶\d+)?")
#: 章节名展示上限。Results 的小节标题常是一整句话。
_SECTION_TITLE_MAX = 30


def _section_titles(out_dir: Path) -> dict[str, str]:
    """读 `parsed.json` 建「章节 id → 标题」表。

    `§2.1.1.5.1.1` 这种自动编号对人毫无意义——溯源的全部价值就是能回原文核对，
    显示成一串数字等于没有溯源。这里换成人能认的章节名（`Methods › Mice`）。
    """
    p = out_dir / "parsed.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    by_id = {s["id"]: s for s in data.get("sections", [])}
    out: dict[str, str] = {}
    for sid, s in by_id.items():
        title = (s.get("title") or "").strip()
        if not title:
            continue
        # 上级若是 Methods / Results 这类顶层名，拼成面包屑更好定位
        parent = by_id.get(sid.rsplit(".", 1)[0]) if "." in sid else None
        pt = (parent or {}).get("title", "").strip()
        if pt and len(pt) <= 12 and pt.lower() != title.lower():
            title = f"{pt} › {title}"
        if len(title) > _SECTION_TITLE_MAX:
            title = title[:_SECTION_TITLE_MAX] + "…"
        out[sid] = title
    return out


def _readable_refs(items: list[str], titles: dict[str, str]) -> list[str]:
    """把 `§2.1.1.5.1.1¶2` 换成 `Methods › Mice ¶2`；查不到就原样保留。"""
    if not titles:
        return items

    def sub(m: re.Match) -> str:
        t = titles.get("§" + m.group(1))
        return f"{t} {m.group(2)}" if t and m.group(2) else (t or m.group(0))

    return [_SECTION_REF_RE.sub(sub, s) for s in items]


def _clean(value: str) -> str:
    """`not_reported（理由）` → `未说明（理由）`；纯哨兵词 → `未说明`。

    理由必须留着——它证明抽取时确实查过而非漏填，复核时省一次回溯。
    """
    if not value:
        return NOT_REPORTED_ZH
    s = value.strip()
    if is_reported(s):
        return s
    m = _NR_REASON_RE.match(s)
    return f"{NOT_REPORTED_ZH}（{m.group(1)}）" if m else NOT_REPORTED_ZH


def _short_title(caption: str) -> str:
    """取图题的标题句：去掉图号前缀，截到第一个子图标号或句末。"""
    t = _LABEL_RE.sub("", caption or "").strip()
    t = re.split(r"(?<=[.。])\s|(?<=\.)\s*[a-z]\s*,", t)[0]
    return t.strip(" .") or "（无图题）"


def _thumbnail(path: Path) -> str:
    """切图 → base64 JPEG data URI。Pillow 缺失或读失败时返回空串（页面自动省略图片）。"""
    try:
        from PIL import Image
    except ImportError:
        return ""
    if not path.is_file():
        return ""
    try:
        im = Image.open(path).convert("RGB")
        if im.width > THUMB_WIDTH:
            h = round(im.height * THUMB_WIDTH / im.width)
            im = im.resize((THUMB_WIDTH, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=THUMB_QUALITY, optimize=True, progressive=True)
    except Exception:  # noqa: BLE001 — 缩略图失败不该拖垮整页
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def build_page_data(bundle: PaperBundle, out_dir: Path) -> dict[str, Any]:
    """`PaperBundle` → 页面用的紧凑结构。键名压到 1~3 字符，2 MB 里大头是图。"""
    reasons = review_reasons(bundle)
    titles = _section_titles(out_dir)
    figs: list[dict[str, Any]] = []
    n_rep = n_tot = st_rep = st_tot = 0

    for f in bundle.figures:
        panels = []
        for p in f.panels:
            st_tot += 1
            if is_reported(p.stats.test):
                st_rep += 1
            groups = []
            for g in p.groups:
                n_tot += 1
                if is_reported(g.n):
                    n_rep += 1
                groups.append({
                    "n": g.name, "r": g.role, "c": _clean(g.condition),
                    "nn": _clean(g.n), "ro": _clean(g.readout),
                    "res": _clean(g.result), "vc": _clean(g.vs_control),
                })
            panels.append({
                "l": p.label, "e": p.experiment,
                "iv": _clean(p.design.independent_var), "lv": p.design.levels,
                "u": _clean(p.design.unit), "ctl": p.design.controlled,
                "st": [_clean(p.stats.test), _clean(p.stats.error_bar),
                       _clean(p.stats.replicates), _clean(p.stats.significance)],
                "f": p.finding, "inf": p.evidence.inferred,
                "src": {
                    "cap": p.evidence.from_caption,
                    "body": _readable_refs(p.evidence.from_body, titles),
                    "meth": _readable_refs(p.evidence.from_methods, titles),
                    "img": p.evidence.from_image_only,
                },
                "g": groups,
            })
        rel = _RELATED_RE.search(f.caption or "")
        num = re.search(r"(\d+)", f.figure_id)
        figs.append({
            "id": f.figure_id, "ext": f.figure_id.startswith("Extended"),
            "num": int(num.group(1)) if num else 0,
            "rel": int(rel.group(1)) if rel else None,  # 附图挂在哪张正文图下
            "t": _short_title(f.caption), "q": f.question, "c": f.figure_conclusion,
            "conf": f.confidence.value, "kind": f.figure_kind, "pg": f.pages,
            "oq": f.open_questions, "p": panels,
            "img": _thumbnail(out_dir / f.images[0]) if f.images else "",
            "rev": [{"k": r.kind, "p": r.panel, "t": r.text}
                    for r in reasons.get(f.figure_id, [])],
        })

    n_main = sum(1 for f in figs if not f["ext"])
    n_ext = len(figs) - n_main
    return {
        "paper": {
            "title": bundle.paper.title or "（未取到标题）",
            "venue": bundle.paper.venue, "year": bundle.paper.year,
            "doi": bundle.paper.doi,
            "mix": f"正文 {n_main}" + (f" + Extended Data {n_ext}" if n_ext else ""),
        },
        "kpi": {
            "figs": len(figs), "panels": st_tot, "groups": n_tot,
            "nRate": round(n_rep / max(1, n_tot) * 100),
            "stRate": round(st_rep / max(1, st_tot) * 100),
            "review": sum(len(f["rev"]) for f in figs),
        },
        "figs": figs,
    }


def render_page(bundle: PaperBundle, out_dir: Path) -> Optional[Path]:
    """写出 `index.html`。模板缺失时返回 None（不阻断其余渲染）。"""
    if not PAGE_TEMPLATE.is_file():
        return None
    data = build_page_data(bundle, out_dir)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # 内嵌 JSON 里若出现 `</script` 会提前闭合脚本块
    payload = payload.replace("</", "<\\/")

    html = PAGE_TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload)
    dest = out_dir / PAGE_FILENAME
    dest.write_text(html, encoding="utf-8")
    return dest
