"""L3 抽取层 —— 看图 + 读上下文 → 结构化实验设计。

两种模式共用同一套提示词与校验逻辑，本期只实现模式 A：

    模式 A（本期）  build_extraction_tasks() 生成任务文件
                    → Claude Code agent 逐图 Read PNG + 任务文件 → 写 extracted/<slug>.json
                    → merge_records() 汇总成 figures.json
    模式 B（预留）  extract_via_api() 走 OpenAI 兼容端点批量跑，见函数内 TODO

本模块是 PLAN.md §4「两条铁律」的机器可检查部分：

    铁律 1  原文没写就填 not_reported，禁止脑补（n / 剂量 / 统计检验是最高危三项）
    铁律 2  每个字段可溯源，evidence 五桶记录来源

铁律 1 无法被程序完全验证（程序不知道原文有没有写），因此 validate_record() 采用
「填了高危字段却没在 evidence 里溯源 → 报复核警告」的间接检查；
真正的防线是 EXTRACTION_RULES 里的提示词本身。

模块内的提示词常量（EXTRACTION_RULES / FEW_SHOT / SELF_CHECK）是**唯一真相源**，
SKILL.md 里的规则是它的常驻副本，两者由 tests/test_extract.py 做漂移检测。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

try:  # 作为包导入：from pfn.extract import ...
    from .models import (
        NOT_REPORTED,
        SCHEMA_VERSION,
        ContextPack,
        FigureRecord,
        PaperBundle,
        PaperMeta,
        Panel,
        is_reported,
    )
except ImportError:  # 直接 `python scripts/pfn/extract.py` 运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from pfn.models import (  # type: ignore[no-redef]
        NOT_REPORTED,
        SCHEMA_VERSION,
        ContextPack,
        FigureRecord,
        PaperBundle,
        PaperMeta,
        Panel,
        is_reported,
    )

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SCHEMA_PATH = PROJECT_ROOT / "schemas" / "figure.schema.json"

TASKS_DIR = "tasks"
EXTRACTED_DIR = "extracted"


# ════════════════════════════════════════════════════════════════════════════
#  提示词 —— 本项目最核心的产出，改动前请先读 PLAN.md §4
# ════════════════════════════════════════════════════════════════════════════

EXTRACTION_RULES = """\
### 铁律 1 · 原文没写就填 `not_reported`，严禁猜测

最高危的三项是 **`groups[].n`**、**`condition` 里的剂量 / 超参数值**、**`stats.test`**。
文献整理里模型最爱在这三处脑补，而它们一旦编错，整张汇总表就不可信了。

**下笔前的强制检查**：写下任何一个具体数值之前，先回答「它出现在哪？」——
只有四个合法答案：

1. 图题的第几句话
2. 正文的哪一段（`§2.3¶1` 这样的段号）
3. Methods 的哪一节（`§4.2`）
4. 图上的什么位置（哪根坐标轴 / 哪个图例 / 图上印着的数字 / 哪个显著性标记）

答不出来 → 一律填 `not_reported`。

**明令禁止的推理**（下面每一条都是脑补，不是证据）：

- 「这类实验通常 n=3」「CCK-8 一般做三复孔」「动物实验一般每组 6 只」
- 「多组比较通常用 ANOVA」「两组比较应该是 t-test」
- 「误差棒一般画的是 SD」
- 「ML 论文一般跑 3 个 random seed」
- 「这个药在同类研究里常用 10 mg/kg」

领域常识**不是本文的数据**。同理，别的 panel 写了 `n=3`，也不能顺手抄给这个 panel——
每个 panel 单独溯源。

**Methods 片段是关键词召回来的，未必对应本图的实验。** 引用「Methods 相关片段」一节
之前，先确认它讲的就是这张图做的事（同一个 assay、同一批细胞/动物）；
只是术语撞上了就别用——张冠李戴的剂量比空白更糟。

### 反过来的错误：图题写了却填 `not_reported`

铁律 1 防的是「无中生有」，这一条防的是「视而不见」——两者一样是错的。

图题的**总述**（第一个 `(A)` 之前）和**收尾**（最后一个子图描述之后）讲的是**整张图**，
对 **A~Z 每一个 panel 都成立**。而最高价值的信息恰恰常年藏在收尾里：

```
* indicates p < 0.05, ** p < 0.01, *** p < 0.001. N = 3.
The control group was used for comparison. Data are expressed as mean ± SD.
```

这一段同时交代了高危三项里的两项——`n` 和 `stats`。**它不属于最后那个子图**，
把它只算给 panel D，A/B/C 就会白白填成 `not_reported`。

所以：**把任何 panel 的 `n` / `stats.error_bar` / `stats.significance` 填成
`not_reported` 之前，先回头确认图题的总述与收尾里没有写 `N = ...`、`mean ± SD`、
`* p<0.05` 这类内容。** 有就逐个 panel 都填上，`evidence` 记 `from_caption`
（这是原文明示，不是推断，不要进 `inferred`）。

**`not_reported` 是合格答案，不是失败。** 空白会被人工复核时补上；
一个看起来很合理的假数字不会被任何人质疑，危害大得多。

**描述你看见的事实，而不是它的名字**：图上有误差棒但没标类型，
就写 `error_bar: "图中有误差棒，类型未标注"`，不要写 `"SD"`。

### 铁律 2 · 每个字段必须可溯源

`evidence` 的五个桶各装**字段路径**（如 `groups[].n`、`stats.significance`、
`design.independent_var`）或可辨认的短标签（如 `IC50 数值`）：

| 桶 | 装什么 |
|----|--------|
| `from_caption` | 图题里写明的字段 |
| `from_body` | 正文引用段给出的字段，**同时把段号写进去**，如 `§2.3¶1` |
| `from_methods` | Methods 章节给出的字段，写节号如 `§4.2` |
| `from_image_only` | 只能靠读图得到的：图上印的数值、柱高、象限百分比、星号、坐标轴范围 |
| `inferred` | **模型推断的** ← 人工复核只看这一栏 |

判定规则：**只要一个值不是原文逐字给出，它就必须进 `inferred`**，宁滥勿缺。
`inferred` 多写几条只是让人多看两眼，漏写一条却会让假数据混进综述。

推断值本身还要**自带依据**——把理由写进值里：

```
"n": "3（图中散点为 3 个）"           ✅ 值 + 依据，且 evidence.inferred 含 groups[].n
"n": "3"                              ❌ 看不出这个 3 从哪来
"replicates": "3（据散点数目推断）"    ✅
```

**数值照抄原样**：保留单位与小数位，不换算、不四舍五入。
图上印着 `86.63%` 就写 `86.63%`；只能目测柱高就写 `约 75%`，
不要写成 `75.0%` 假装精确。

### 领域判定（`domain`）

先看图题 + 上下文，判定这张图属于哪套 profile，再按对应列填 `design` / `groups`：

| | `bio` | `mlcs` |
|---|---|---|
| 识别信号 | 细胞株、动物、μM / mg·kg⁻¹、WB / qPCR / 流式 / IHC / 生存曲线 | 数据集、baseline、ablation、超参、Accuracy / F1 / BLEU / 困惑度 |
| 分组 = | 处理组 / 对照组 | baseline / ablation / 变体 |
| `n` = | 动物数、孔数、生物学重复数 | seeds、runs |
| `condition` = | 药物 + 剂量 + 途径 + 时长 | 模型 + 数据集 + 超参 |
| `readout` = | WB / qPCR / IHC / 流式 / 生存曲线 | Accuracy / F1 / BLEU / 困惑度 |
| `unit` = | 动物 / 细胞孔 / 样本 | 模型 / 数据集 / 运行 |

交叉学科论文（如 AI4Science）按**这张图本身测什么**判定，而不是按论文整体基调。
任务文件顶部若写了「强制 profile」，直接用它，不要再自行判定。

### `figure_kind` 与 panel 拆分

`panels` 是分析单位——**一张图常含多个互相独立的实验，必须拆开**。

- `experiment`：有分组、有对照的实验图 → 按图题里的 `(A)(B)(C)` 和图上的标号逐个拆 panel
- `analysis`：对已有数据的再分析（火山图、富集分析、相关性、消融汇总表）→ 照样拆 panel
- `qualitative_example`：代表性图像 / 案例展示（免疫荧光代表图、生成样例）→ 有分组就填，没有就只写 `finding`
- `schematic`：机制示意图 / 模型架构图，**不含实验数据** → `panels` 留空数组 `[]`，
  只写 `question` 和 `figure_conclusion`（把示意的逻辑链写清楚）

拆分细则：

- 同一 panel 内有多个细胞株 / 多个数据集，若**共用同一套自变量** → 作为 `groups` 的不同行
  （组名写成 `U251 · 12 h` 这样的复合名）；若自变量本身不同 → 拆成两个 panel
- 整图不分子图 → 一个 panel，`label` 填 `"-"`
- 图题里有 (D)(E) 但切图上看不到 → 照样建 panel，`finding` 按图题写，
  并在 `open_questions` 里注明「切图可能不完整」

### `role` 怎么选（跨文献长表按它来筛，别随手填）

| 取值 | 什么时候用 |
|------|-----------|
| `control` | 溶剂 / 空载 / 假手术 / 未处理对照——「什么都不做」的那一组 |
| `treatment` | 受试处理组（加药、敲低、过表达、干预） |
| `baseline` | 对照的是**已有方法**而非「不处理」（`mlcs` 里的对比模型） |
| `ablation` | 从完整方法里拿掉一个组件（`w/o retrieval` 这类） |
| `reference` | 只作参照、不参与主结论的组（阳性药、上限 oracle、公开报告值） |

对照组自己的 `vs_control` 统一写 `"基线"`，不要留空、也不要写 `not_reported`。

### 读图清单（逐条过一遍，别只看图题就下笔）

上下文里没有的数值**只能从图上读**，这是本工具相对「只读文字」的全部价值所在：

1. **坐标轴**：名称 + 单位 + 刻度范围（`levels` 常常就藏在横轴标签里）
2. **图例**：分组名**照抄原文**，不要翻译（跨文献汇总要靠它对齐）
3. **误差棒**：有没有？标没标类型？
4. **显著性标记**：`*` `**` `***` `ns` 标在**哪两组之间**，一并写进 `stats.significance`
5. **图上印着的数字**：IC50、流式象限百分比、AUC、准确率——优先照抄，比目测柱高可靠得多
6. **散点个数**：常暗示重复数，但必须走 `inferred` 且自带依据
7. **色标 / 热图**：数值范围与方向
8. **切图完整性**：任务文件若标了低置信度，先确认图有没有被截断；截断了写进 `open_questions`

### 语言与命名

- 叙述性字段（`question` / `experiment` / `finding` / `figure_conclusion` / `open_questions`）写**中文**
- **标识性字段保留原文**：组名、基因名、药名、细胞株、模型名、数据集名、指标名
  （`Annexin V-FITC`、`U251`、`HMOX1`、`BLEU`——翻译了就没法跨文献比对）

### `confidence` 打分标准

| 取值 | 什么时候用 |
|------|-----------|
| `high` | 分组、指标、结果三者来源明确，读图清晰，推断很少 |
| `medium` | 结果靠目测柱高，或 `design` 有一部分靠推断 |
| `low` | 切图不完整 / 图题极简 / 大面积 `not_reported` / 看不清坐标轴 |

### `open_questions`

写**影响可重复性的缺失**，一条一句。最常见的三类正是高危三项：
n 未报告、误差棒类型未标注、统计方法未说明。
这一栏不是凑数——它是「这篇论文哪里交代不清」的直接产出。
"""


FEW_SHOT = """\
下面是同一张真实论文图（Fig. 1，CCK-8 + 流式凋亡）的抽取片段。
它示范了三件最容易做错的事：**从图上读 IC50**、**照抄流式象限百分比**、
**从散点数推断 n 并如实标记为 inferred**。

#### ✅ 正例 A · 数值只能从图上读时

```json
{
  "label": "A",
  "experiment": "CCK-8 法测定 DMC-GF 对两株胶质瘤细胞的剂量-时间依赖性杀伤，拟合 IC50",
  "design": {
    "independent_var": "DMC-GF 浓度 × 处理时长",
    "levels": ["0-4.5 μM 梯度", "12 h", "24 h", "48 h"],
    "controlled": ["细胞株 U251 / U87 分别独立测定", "同批次接种密度"],
    "unit": "细胞孔"
  },
  "groups": [
    {
      "name": "U251 · 24 h",
      "role": "treatment",
      "condition": "DMC-GF 0-4.5 μM 梯度，处理 24 h",
      "n": "not_reported",
      "readout": "细胞活力（CCK-8, %）",
      "result": "IC50 = 3.205 μM；4.5 μM 时活力约 33%",
      "vs_control": "较 12 h 组 IC50 下降 38%"
    }
  ],
  "stats": {
    "test": "not_reported",
    "error_bar": "图中有误差棒，类型未标注",
    "replicates": "not_reported",
    "significance": "not_reported"
  },
  "finding": "DMC-GF 对 U251 与 U87 均呈剂量与时间依赖性杀伤，48 h IC50 约 2.7 μM",
  "evidence": {
    "from_caption": ["experiment", "design.independent_var"],
    "from_body": ["§2.3¶1"],
    "from_methods": ["§4.2"],
    "from_image_only": ["groups[].result", "IC50 数值"],
    "inferred": ["design.controlled"]
  }
}
```

要点：`IC50 = 3.205 μM` 是图上印出来的 → 进 `from_image_only`，照抄三位小数；
`4.5 μM 时活力约 33%` 是目测柱高 → 写「约」；
`n` 图题正文都没写 → `not_reported`，**没有编 3**；
误差棒看得见但没标类型 → 如实描述；
`design.controlled` 是根据图的排布推断的 → 进 `inferred`。

#### ✅ 正例 B · 图上有精确数字时照抄

```json
{
  "name": "5 μM", "role": "treatment", "condition": "DMC-GF 5 μM", "n": "not_reported",
  "readout": "Annexin V+/PI+ 象限占比",
  "result": "Q1-UR 86.63%，活细胞仅 5.63%",
  "vs_control": "接近全部凋亡"
}
```

流式图每个象限都印着百分比 → 逐字照抄 `86.63%`，不要写成「约 87%」。

#### ✅ 正例 C · 允许推断，但必须标出来（最关键的一例）

```json
{
  "label": "C",
  "experiment": "凋亡率定量统计（对应 panel B 的柱状汇总）",
  "groups": [
    {
      "name": "3 μM", "role": "treatment", "condition": "DMC-GF 3 μM",
      "n": "3（图中散点为 3 个）",
      "readout": "凋亡率（%）", "result": "约 75%", "vs_control": "*** p<0.001"
    }
  ],
  "stats": {
    "test": "not_reported",
    "error_bar": "图中为误差棒 + 单点散布，类型未标注",
    "replicates": "3（据散点数目推断）",
    "significance": "*** 标注于 0 μM 与 3 μM 之间"
  },
  "evidence": {
    "from_caption": ["experiment"],
    "from_body": [], "from_methods": [],
    "from_image_only": ["groups[].result", "stats.significance"],
    "inferred": ["groups[].n", "stats.replicates"]
  }
}
```

要点：柱子上有 3 个散点 → 可以推断 n=3，但**三件事必须同时做到**：
① 值里带依据「（图中散点为 3 个）」；② `evidence.inferred` 里列出 `groups[].n`；
③ `stats.test` 依然是 `not_reported`——看得见星号不等于知道用了什么检验。

对应的 `open_questions`：

```json
["CCK-8 与流式实验的生物学重复数未在图题或正文中说明",
 "误差棒为 SD 还是 SEM 未标注",
 "凋亡率统计所用检验方法未说明"]
```

#### ❌ 反例 · 同一张图的错误抽法

```json
{
  "groups": [{ "name": "3 μM", "n": "3", "vs_control": "p < 0.001" }],
  "stats": { "test": "t-test", "error_bar": "SD", "replicates": "3 次独立实验" },
  "evidence": { "from_caption": ["experiment"], "inferred": [] }
}
```

四处错误，每一处都足以毁掉这条记录：

1. `n: "3"` —— 数字也许没错，但看不出依据，复核时无从查起（正确：`"3（图中散点为 3 个）"`）
2. `test: "t-test"` —— **原文根本没说**，纯粹是「两组比较应该用 t 检验」的脑补 → 必须 `not_reported`
3. `error_bar: "SD"` —— 图上只有误差棒、没标类型，编了个最常见的答案
4. `inferred: []` —— 上面三条全是推断，却声称零推断。**这是最严重的一条**：
   它让假数据以「原文明示」的身份进入综述，人工复核会直接跳过。
"""


SELF_CHECK = """\
写完 JSON、保存之前，**逐条回答**下面五问。任何一条答不上来，就把对应字段改回 `not_reported`：

1. 每一个不是 `not_reported` 的 **`n`**，我能立刻说出它出自哪句图题、哪个 `§` 段落，
   还是图上什么位置吗？（说不出 → 改回 `not_reported`）
2. **`stats.test`** 若不是 `not_reported`，原文里是否**逐字出现过**这个检验的名字？
   （只看到 `*` 星号 ≠ 知道用了什么检验）
3. `condition` 里的**剂量 / 浓度 / 时长 / 超参**数值，是否都能在图题、正文、Methods
   或图上指认出处？
4. 所有**不是原文逐字给出**的值，是否都进了 `evidence.inferred`？
   （反过来自查：`inferred` 为空的 panel，真的每个字段都有原文出处吗？）
5. 组名是否**照抄了图例 / 横轴的原文**，没有翻译、没有自己起名？

再确认三件事：

- **图题的总述与收尾**（适用全图的那两节）里的 `N = ...`、`mean ± SD`、`* p<0.05` 定义，
  是否已经填进**每一个** panel 的 `n` / `stats`？漏填和编造一样是错的。
- `figure_kind` 是 `schematic` 时，`panels` 必须是空数组 `[]`
- 高危三项凡是填了 `not_reported` 的，是否已在 `open_questions` 里体现？
"""


_TASK_HEADER = """\
# 抽取任务 · {figure_id}

> **产出**：一条 `FigureRecord` JSON，写到
> `{extracted_abs}`
> 单条记录，不是 bundle；契约见 `schemas/figure.schema.json`。
> 全部图抽完后跑 `python scripts/pfn/extract.py merge --out "{out_dir}"` 汇总。

## 0. 先做这两件事

1. **Read 切图**（下面的上下文里没有的数值，全靠读图得到）：
{image_list}
2. 通读「填写规则」之前的全部原文各节，再动笔。图题若切出了**总述**或**收尾**，
   那两节适用于**每一个 panel**，别漏。

{flags}
"""


def _fmt_paragraphs(paragraphs: list[dict[str, Any]]) -> str:
    if not paragraphs:
        return "_（无）_"
    lines: list[str] = []
    for p in paragraphs:
        pid = p.get("id", "?")
        page = p.get("page", "?")
        lines.append(f"**`{pid}`**（p.{page}）\n")
        lines.append(f"> {p.get('text', '').strip()}\n")
    return "\n".join(lines)


def _fmt_grid(grid: list[list[str]]) -> str:
    lines = ["读图看错小数点时以这里为准：\n", "```"]
    for row in grid:
        lines.append(" | ".join("" if c is None else str(c) for c in row))
    lines.append("```")
    return "\n".join(lines)


def record_slug(figure_id: str) -> str:
    """`Figure 1` → `figure_01`，`Table 3` → `table_03`，`Figure S1` → `figure_s1`。

    任务文件、抽取结果文件都用它命名，保证 merge 时能与 context 对上。
    """
    tokens = [t for t in re.split(r"[^0-9A-Za-z一-鿿]+", figure_id.strip()) if t]
    if not tokens:
        return "figure_xx"
    if tokens[-1].isdigit():
        tokens[-1] = tokens[-1].zfill(2)
    return "_".join(t.lower() for t in tokens)


def _skeleton(pack: ContextPack, paper: PaperMeta, profile: Optional[str]) -> str:
    """预填 L1/L2 已知字段，agent 只需补「智能部分」，减少转抄错误。"""
    skel: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "paper": paper.model_dump(mode="json"),
        "domain": profile or "<bio | mlcs，见「领域判定」>",
        "figure_id": pack.figure_id,
        "kind": pack.kind,
        "pages": pack.pages,
        "images": pack.images,
        "caption": pack.caption,
        "figure_kind": "<experiment | schematic | qualitative_example | analysis>",
        "question": "<这张图要回答什么问题 / 验证什么假设>",
        "panels": [
            {
                "label": "A",
                "experiment": "<一句话说清做了什么实验>",
                "design": {
                    "independent_var": "<被操纵的因素 = 分组依据>",
                    "levels": ["<水平1>", "<水平2>"],
                    "controlled": ["<保持固定的条件>"],
                    "unit": "<动物 / 细胞孔 / 样本 / 模型 / 数据集>",
                },
                "groups": [
                    {
                        "name": "<照抄图例或横轴原文>",
                        "role": "<treatment | control | baseline | ablation | reference>",
                        "condition": "<药物+剂量+途径+时长 / 模型+数据集+超参>",
                        "n": NOT_REPORTED,
                        "readout": "<测量指标 + 方法>",
                        "result": "<数值 + 方向>",
                        "vs_control": "<相对对照的变化 / 显著性>",
                    }
                ],
                "stats": {
                    "test": NOT_REPORTED,
                    "error_bar": NOT_REPORTED,
                    "replicates": NOT_REPORTED,
                    "significance": NOT_REPORTED,
                },
                "finding": "<该 panel 的结论>",
                "evidence": {
                    "from_caption": [],
                    "from_body": [],
                    "from_methods": [],
                    "from_image_only": [],
                    "inferred": [],
                },
            }
        ],
        "figure_conclusion": "<整图结论>",
        "confidence": "<high | medium | low>",
        "open_questions": ["<原文未交代、影响可重复性的点>"],
    }
    body = json.dumps(skel, ensure_ascii=False, indent=2)
    return (
        "`schema_version` / `paper` / `figure_id` / `kind` / `pages` / `images` / `caption` "
        "已由前面两层填好，**照原样保留**（尤其 `images` 是相对路径，不要换成绝对路径）。\n"
        "`<...>` 是待填占位符。panel 有几个就写几个，不要被骨架里的一个限制。\n\n"
        f"```json\n{body}\n```"
    )


#: 抽取档位。**token 全部花在 L3 读图**（L1/L2/L4 是纯 Python，加起来 2 秒、零 token），
#: 所以省钱只能从「少抽字段」和「少抽图」下手，而不是「少生成文件」。
EXTRACT_LEVELS: dict[str, dict[str, str]] = {
    "light": {
        "name": "轻量",
        # 实测：输入侧砍掉 Methods 原文与正反例后为完整档的 76%；输出侧只写四五个字段，
        # 约为完整档的 15~20%。读图那部分是每张图的固定开销，档位省不掉，只能靠 --figures。
        "cost": "输入 ≈76% · 输出 ≈15~20% · 合计约省一半",
        "fill": "`figure_kind` / `question` / 每个 panel 的 `label`、`experiment`、`finding` / `figure_conclusion`",
        "skip": "`groups` 留空数组 `[]`、`design` 与 `stats` 全部填 `not_reported`、"
                "`evidence` 五个桶全留空、`open_questions` 留空数组",
        "use": "只想知道「每张图做了什么、得出什么结论」——初筛大批文献、判断要不要精读时用这档",
    },
    "standard": {
        "name": "标准",
        "cost": "输入 ≈92% · 输出 ≈50% · 合计约省四分之一",
        "fill": "轻量档的全部，外加每个 panel 的 `design`（自变量/水平/实验单位）与 "
                "`groups`（`name` / `role` / `condition` / `result`）",
        "skip": "`groups[].n`、`groups[].readout`、`groups[].vs_control`、`stats` 四项、"
                "`evidence` 五个桶、`open_questions` —— 一律 `not_reported` 或留空",
        "use": "要看清分组与结论，但不需要 n、统计方法和逐字段溯源",
    },
    "full": {
        "name": "完整",
        "cost": "基准",
        "fill": "全部字段",
        "skip": "无",
        "use": "要做可重复性核查或跨文献汇总——n、统计方法、逐字段溯源缺一不可",
    },
}
DEFAULT_LEVEL = "full"


#: 各档位要从任务文件里**删掉**的小节（按标题前缀匹配）。
#: 光加一句「本档不用填 n」省不了输入 token——规则、Methods 原文、正反例照样发过去。
#: 实测占比：Methods 片段 20% · 正文引用段 16% · 正反例 14% · 输出骨架 12%。
_DROP_SECTIONS: dict[str, tuple[str, ...]] = {
    "light": (
        "## 6. Methods",          # 不填 condition/n/stats 就用不到方法学原文
        "## 8. 正例与反例",        # few-shot 通篇在讲 n 推断与溯源
        "### 铁律 2",             # 不填 evidence
        "### 反过来的错误",        # 讲的是 n 漏填
        "### `role` 怎么选",      # 不建 groups
        "### 读图清单",           # 只要「做了什么+结论」，不必逐项读数
        "## 10. 提交前自检",       # 自检项全是 n / stats / 溯源
    ),
    "standard": (
        "### 铁律 2",             # 不填 evidence
        "### 反过来的错误",
        "## 10. 提交前自检",
    ),
}

_SECTION_RE = re.compile(r"^(#{2,3}) (.+)$", re.M)


def _trim_sections(md: str, level: str) -> str:
    """按档位删掉用不到的小节。`##` 连同其下 `###` 一起删；`###` 删到下一个同级或上级。"""
    drops = _DROP_SECTIONS.get(level)
    if not drops:
        return md
    marks = [(m.start(), len(m.group(1)), m.group(0)) for m in _SECTION_RE.finditer(md)]
    if not marks:
        return md
    spans: list[tuple[int, int]] = []
    for i, (pos, lvl, title) in enumerate(marks):
        if not any(title.startswith(d) for d in drops):
            continue
        end = len(md)
        for pos2, lvl2, _ in marks[i + 1:]:
            if lvl2 <= lvl:          # 同级或更高级标题即为本节终点
                end = pos2
                break
        spans.append((pos, end))
    for a, b in sorted(spans, reverse=True):
        md = md[:a] + md[b:]
    return re.sub(r"\n{4,}", "\n\n\n", md)


def _level_directive(level: str) -> str:
    """插在任务文件最前面的档位说明。放最前是因为它**覆盖**后面的通用规则。"""
    spec = EXTRACT_LEVELS.get(level) or EXTRACT_LEVELS[DEFAULT_LEVEL]
    if level == DEFAULT_LEVEL:
        return ""
    return (
        f"> ## ⚑ 本次抽取档位：{spec['name']}（{spec['cost']}）\n>\n"
        f"> **只填**：{spec['fill']}\n>\n"
        f"> **跳过**：{spec['skip']}\n>\n"
        f"> 跳过的字段按 schema 要求保留键、填 `not_reported` 或空数组，**不要删键**，"
        f"否则汇总会判为结构非法。\n>\n"
        f"> 这一节**覆盖**下文的通用规则：下面写「必须填 n / 必须逐字段溯源」的地方，"
        f"本档一律不适用。两条铁律里「不许编造」仍然有效——跳过不等于可以瞎填。\n\n"
    )


def _select_packs(packs: list[ContextPack], spec: Optional[str]) -> list[ContextPack]:
    """按 `--figures` 挑图。空 spec 表示全要。

    「系列」与「图号」是**与**关系，各自缺省表示不限：

    - `main` → 全部正文图      · `ed` → 全部 Extended Data
    - `1-4`  → 两个序列里图号 1~4 的（正文 4 张 + ED 4 张）
    - `main,1-4` → 只要正文图的 1~4 共 4 张
    """
    if not spec:
        return packs
    series: set[str] = set()
    nums: set[int] = set()
    for part in spec.replace("，", ",").split(","):
        p = part.strip().lower()
        if not p:
            continue
        if p in ("main", "正文"):
            series.add("main")
        elif p in ("ed", "extended", "附图"):
            series.add("ed")
        elif "-" in p:
            a, _, b = p.partition("-")
            if a.strip().isdigit() and b.strip().isdigit():
                nums.update(range(int(a), int(b) + 1))
        elif p.isdigit():
            nums.add(int(p))

    def keep(pk: ContextPack) -> bool:
        is_ed = pk.figure_id.startswith("Extended")
        if series and ("ed" if is_ed else "main") not in series:
            return False
        if nums:
            m = re.search(r"(\d+)", pk.figure_id)
            if not m or int(m.group(1)) not in nums:
                return False
        return True

    return [p for p in packs if keep(p)]


def build_extraction_tasks(
    out_dir: Path,
    *,
    profile: Optional[str] = None,
    paper: Optional[PaperMeta] = None,
    level: str = DEFAULT_LEVEL,
    figures: Optional[str] = None,
) -> list[Path]:
    """读 `context.json`，为每张图生成一个自包含的抽取任务文件。

    任务文件把「切图路径 + 上下文原文 + schema 骨架 + 填写规则 + 正反例 + 自检清单」
    打包在一起，因此可以直接丢给一个没有别的上下文的 subagent 执行。

    Args:
        out_dir: prep 的输出目录（含 context.json / figures/）
        profile: `bio` / `mlcs`，非空则跳过领域自动判定（`--profile` 强制覆盖）
        paper: 论文元信息；缺省时从 `parsed.json` 取标题

    Returns:
        写出的任务文件路径，按 context.json 顺序。
    """
    out_dir = Path(out_dir).resolve()
    ctx_file = out_dir / "context.json"
    if not ctx_file.is_file():
        raise FileNotFoundError(
            f"找不到 {ctx_file}。请先跑：python scripts/paper_figure_notes.py prep <pdf> --out {out_dir}"
        )

    raw = json.loads(ctx_file.read_text(encoding="utf-8"))
    packs = [ContextPack.model_validate(p) for p in raw]

    total = len(packs)
    packs = _select_packs(packs, figures)
    if not packs:
        raise ValueError(f"--figures {figures!r} 没选中任何图（共 {total} 张）")

    if paper is None:
        paper = _paper_meta_from_parsed(out_dir)

    tasks_dir = out_dir / TASKS_DIR
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / EXTRACTED_DIR).mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for pack in packs:
        slug = record_slug(pack.figure_id)
        task_path = tasks_dir / f"{slug}.md"
        task_path.write_text(
            _level_directive(level)
            + _trim_sections(_render_task(pack, out_dir, slug, paper, profile), level),
            encoding="utf-8",
        )
        written.append(task_path)
    return written


def _render_task(
    pack: ContextPack,
    out_dir: Path,
    slug: str,
    paper: PaperMeta,
    profile: Optional[str],
) -> str:
    images = "\n".join(
        f"   - `{(out_dir / img).as_posix()}`" for img in pack.images
    ) or "   - _（本图无切图，只能靠文字上下文，`confidence` 应为 `low`）_"

    flags: list[str] = []
    if profile:
        flags.append(
            f"> **强制 profile：`{profile}`** —— 直接填 `domain: \"{profile}\"`，跳过领域判定。"
        )
    conf = getattr(pack.bbox_confidence, "value", str(pack.bbox_confidence))
    if conf != "high":
        flags.append(
            f"> ⚠ **切图置信度 `{conf}`** —— 图可能被截断或多切了旁边的内容。"
            f"先确认图是否完整；若不完整，在 `open_questions` 里写明，并把 `confidence` 降到 `low`。"
        )
    if pack.kind == "table":
        flags.append(
            "> 这是**表格**。`figure_kind` 通常填 `analysis`；"
            "每一行/列的对比条件就是分组，数字以第 5 节的文本网格为准。"
        )

    # 章节动态编号：总述 / 收尾 / 表格网格都可能缺席，写死序号会断档
    sections: list[tuple[str, str]] = [
        ("图题原文", f"> {pack.caption.strip() or '_（缺失）_'}")
    ]

    if pack.caption_preamble.strip():
        sections.append((
            "图题总述 —— **适用于全图每一个 panel**",
            "第一个 `(A)` 之前的总述，讲的是整张图共同的实验设置：\n\n"
            f"> {pack.caption_preamble.strip()}\n\n"
            "**每个 panel 都要用上它**，不要只算在 panel A 头上。",
        ))

    if pack.panel_captions:
        rows = "\n".join(
            f"| `{k}` | {v.strip()} |" for k, v in pack.panel_captions.items()
        )
        sections.append((
            "子图描述（从图题切分）",
            "**每个子图至少对应一个 panel。**\n\n"
            f"| panel | 图题描述 |\n|---|---|\n{rows}",
        ))
    else:
        sections.append((
            "子图描述（从图题切分）",
            "_（图题未切出子图。请按图上的 (A)(B)(C) 标号自行拆 panel；"
            "整图确实只有一个实验时用一个 panel、`label` 填 `\"-\"`。）_",
        ))

    if pack.caption_trailer.strip():
        sections.append((
            "图题收尾 —— **适用于全图每一个 panel**",
            "最后一个子图描述**之后**的收尾文字，讲的同样是整张图：\n\n"
            f"> {pack.caption_trailer.strip()}\n\n"
            "⚠ **这一节是 `n` 与 `stats` 最常见的出处**——`N = 3`、`mean ± SD`、"
            "`* p<0.05` 的定义几乎总是写在这里，且对 **A~Z 每一个 panel 都成立**。\n"
            "它**不属于最后那个子图**。把任何 panel 的 `n` / `stats.*` 填成 "
            "`not_reported` 之前，先回来看这一节——图题写了却漏填，和编造一样是错的。\n"
            "这里取到的值，`evidence` 记 `from_caption`。",
        ))

    sections.append((
        "正文引用段（`from_body` 填这里的段号）",
        _fmt_paragraphs([p.model_dump(mode="json") for p in pack.citing_paragraphs]),
    ))
    sections.append((
        "Methods 相关片段（`from_methods` 填这里的节号）",
        _fmt_paragraphs([p.model_dump(mode="json") for p in pack.methods_paragraphs]),
    ))
    if pack.table_text_grid:
        sections.append(("表格文本网格（数字交叉校验用）", _fmt_grid(pack.table_text_grid)))

    sections.append(("填写规则", EXTRACTION_RULES))
    sections.append(("正例与反例", FEW_SHOT))
    sections.append(("输出骨架", _skeleton(pack, paper, profile)))
    sections.append(("提交前自检", SELF_CHECK))

    body = "\n\n".join(
        f"## {i}. {title}\n\n{content.strip()}\n"
        for i, (title, content) in enumerate(sections, 1)
    )
    return _TASK_HEADER.format(
        figure_id=pack.figure_id,
        extracted_abs=(out_dir / EXTRACTED_DIR / f"{slug}.json").as_posix(),
        out_dir=out_dir.as_posix(),
        image_list=images,
        flags="\n>\n".join(flags),
    ) + "\n" + body


def _paper_meta_from_parsed(out_dir: Path) -> PaperMeta:
    """从 parsed.json 取论文标题；拿不到就返回空 meta（字段都有默认值）。"""
    parsed = out_dir / "parsed.json"
    if not parsed.is_file():
        return PaperMeta()
    try:
        data = json.loads(parsed.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return PaperMeta()
    return PaperMeta(title=str(data.get("title", "") or ""))


# ════════════════════════════════════════════════════════════════════════════
#  校验与降级
# ════════════════════════════════════════════════════════════════════════════

_DELETE = object()  # 哨兵：该键/元素整体不可用，删掉

#: 字段名 → 降级值。FigureRecord 这棵树里字段名无冲突，故用名字而非全路径索引。
#: `figure_id` 故意不在表里——没有标识的记录无法归档，属于不可恢复错误。
_FIELD_FALLBACKS: dict[str, Any] = {
    # FigureRecord
    "schema_version": SCHEMA_VERSION,
    "paper": {},
    "domain": "mlcs",
    "kind": "figure",
    "pages": [],
    "images": [],
    "caption": "",
    "figure_kind": "experiment",
    "question": NOT_REPORTED,
    "panels": [],
    "figure_conclusion": NOT_REPORTED,
    "confidence": "low",  # 校验都没过，不配拿 medium
    "open_questions": [],
    # PaperMeta
    "title": "",
    "year": None,
    "venue": "",
    "doi": "",
    # Panel
    "label": "-",
    "experiment": NOT_REPORTED,
    "design": {},
    "groups": [],
    "stats": {},
    "finding": NOT_REPORTED,
    "evidence": {},
    # Design
    "independent_var": NOT_REPORTED,
    "levels": [],
    "controlled": [],
    "unit": NOT_REPORTED,
    # Group
    "name": NOT_REPORTED,
    "role": "reference",  # 最中性的角色：宁可说不清，也别把对照标成处理
    "condition": NOT_REPORTED,
    "n": NOT_REPORTED,
    "readout": NOT_REPORTED,
    "result": NOT_REPORTED,
    "vs_control": NOT_REPORTED,
    # Stats
    "test": NOT_REPORTED,
    "error_bar": NOT_REPORTED,
    "replicates": NOT_REPORTED,
    "significance": NOT_REPORTED,
    # Evidence
    "from_caption": [],
    "from_body": [],
    "from_methods": [],
    "from_image_only": [],
    "inferred": [],
}

_MAX_REPAIR_ROUNDS = 12


def _json_path(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "<root>"


def _resolve_parent(data: Any, loc: list[Any]) -> Optional[Any]:
    """走到 loc[-1] 的父容器；路径不可达返回 None。"""
    node = data
    for part in loc[:-1]:
        if isinstance(node, dict) and isinstance(part, str) and part in node:
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            node = node[part]
        else:
            return None
    key = loc[-1]
    if isinstance(node, dict) and isinstance(key, str):
        return node
    if isinstance(node, list) and isinstance(key, int):
        return node
    return None


def _coerce(value: Any, fallback: Any) -> Any:
    """尽量保住信息：`n: 3` → `"3"`，`levels: "a"` → `["a"]`，实在不行才用 fallback。

    转换后仍可能不合法（如 `pages: "abc"` → `["abc"]`），下一轮会把烂掉的元素删掉，
    最终一定收敛到 fallback。
    """
    scalar = isinstance(value, (int, float, str)) and not isinstance(value, bool)
    if isinstance(fallback, str) and scalar and not isinstance(value, str):
        return str(value)
    if isinstance(fallback, list) and scalar:
        return [value]
    return copy.deepcopy(fallback)


def _apply_repair(data: dict[str, Any], err: dict[str, Any]) -> Optional[str]:
    """把一条 pydantic 错误就地降级，返回给人看的 warning；无法定位则返回 None。"""
    raw_loc = list(err.get("loc", ()))
    if not raw_loc:
        return None

    # 联合类型的错误 loc 末尾可能挂着类型标签（非真实数据路径），逐级右截
    parent = None
    loc: list[Any] = raw_loc
    for cut in range(len(raw_loc), 0, -1):
        candidate = raw_loc[:cut]
        parent = _resolve_parent(data, candidate)
        if parent is not None:
            loc = candidate
            break
    if parent is None:
        return None

    key = loc[-1]
    path = _json_path(tuple(loc))
    etype = err.get("type", "?")

    if isinstance(key, int):  # 列表元素整体烂掉 → 删除
        if 0 <= key < len(parent):
            parent.pop(key)
            return f"[修复] {path}: 列表元素无法解析（{etype}）→ 已丢弃该元素"
        return None

    if key not in _FIELD_FALLBACKS:
        if etype == "missing":  # 表里没有的必填字段（如 figure_id）无从补起
            return None
        parent.pop(key, None)
        return f"[修复] {path}: 未知字段且无法解析（{etype}）→ 已移除"

    old = parent.get(key, "<缺失>")
    new = _coerce(parent.get(key), _FIELD_FALLBACKS[key])
    parent[key] = new
    if etype == "missing":
        return f"[修复] {path}: 字段缺失 → 已补默认值 {new!r}"
    return f"[修复] {path}: 取值 {old!r} 不合法（{etype}）→ 已回退 {new!r}"


# ── 溯源审计（铁律的间接检查） ──────────────────────────────────────────────

#: 必须用「孤立的 n」而不是子串 `.n` —— 否则 `groups[].name` 会把 n 的溯源检查糊弄过去。
_N_EVIDENCE_RE = re.compile(
    r"(?:^|[^0-9a-z])n(?![0-9a-z])|样本量|样本数|例数|重复|replicat|seed|\bruns?\b|头数|只数",
    re.I,
)
#: 同理不能用裸 `stats` / `test`：前者被 `stats.significance` 命中，
#: 后者在 mlcs 论文里满地都是（test set / testing）。
_TEST_EVIDENCE_RE = re.compile(
    r"\.test\b|检验|统计方法|统计学|显著性检验|anova|t-?test|wilcoxon|mann|kruskal|"
    r"chi-?squar|卡方|log-?rank|fisher|bootstrap|permutation",
    re.I,
)


def _mentions(entries: list[str], pattern: re.Pattern[str]) -> bool:
    return any(pattern.search(e) for e in entries)


#: 图题（尤其总述 / 收尾段）里一旦出现这些，就说明原文**已经报告**了对应字段。
#: 此时 panel 还填 not_reported 就是漏抽——与脑补方向相反、危害相当的另一种错。
#: 必须允许千分位逗号——图题里的队列量常写成 `n = 1,077`，
#: 只匹配到 `n = 1` 会让复核者按提示回填成 1，比不提示更糟。
_CAPTION_N_RE = re.compile(
    r"(?<![A-Za-z])n\s*=\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?|每组\s*\d+\s*[只例头]", re.I
)
_CAPTION_ERRBAR_RE = re.compile(
    r"mean\s*±|±\s*s\.?\s?[de]\.?m?\b|平均值\s*±|均值\s*±|standard\s+(?:deviation|error)",
    re.I,
)
_CAPTION_SIG_RE = re.compile(r"\*+.{0,20}?p\s*[<≤]\s*0?\.\d+|p\s*[<≤]\s*0?\.\d+", re.I)


def _all_evidence(panel: Panel) -> list[str]:
    ev = panel.evidence
    return (
        ev.from_caption + ev.from_body + ev.from_methods + ev.from_image_only + ev.inferred
    )


def _audit(rec: FigureRecord) -> list[str]:
    """结构上合法、但很可能有问题的地方。只查能精确判定的几项，避免警告泛滥。"""
    w: list[str] = []

    if rec.figure_kind == "schematic" and rec.panels:
        w.append(
            f"[复核] {rec.figure_id}: figure_kind=schematic 但 panels 非空——"
            f"示意图不含实验分组，请确认是否该改成 experiment/analysis"
        )
    if rec.figure_kind != "schematic" and not rec.panels:
        w.append(
            f"[复核] {rec.figure_id}: figure_kind={rec.figure_kind} 却没有任何 panel——"
            f"抽取可能失败，或该图其实是 schematic"
        )

    # 图题里已经写明的高危字段。图题的总述/收尾适用于全图，因此逐个 panel 都要比对。
    cap_n = _CAPTION_N_RE.search(rec.caption)
    cap_err = _CAPTION_ERRBAR_RE.search(rec.caption)
    cap_sig = _CAPTION_SIG_RE.search(rec.caption)

    has_unreported_risk = False
    for i, p in enumerate(rec.panels):
        tag = f"{rec.figure_id} panels[{i}]({p.label})"
        ev = _all_evidence(p)

        missed: list[str] = []
        if cap_n and p.groups and all(not is_reported(g.n) for g in p.groups):
            missed.append(f"n（图题写了「{cap_n.group(0)}」）")
        if cap_err and not is_reported(p.stats.error_bar):
            missed.append(f"stats.error_bar（图题写了「{cap_err.group(0)}」）")
        if cap_sig and not is_reported(p.stats.significance):
            missed.append(f"stats.significance（图题写了「{cap_sig.group(0)}」）")
        if missed:
            w.append(
                f"[复核] {tag}: 图题已报告却填成 not_reported → {'；'.join(missed)}。"
                f"图题的总述与收尾适用于全图每一个 panel，请回填（evidence 记 from_caption）"
            )

        if not ev:
            w.append(f"[复核] {tag}: evidence 五个桶全为空，违反「逐字段溯源」铁律")

        if rec.figure_kind == "experiment" and not p.groups:
            w.append(f"[复核] {tag}: 实验图但 groups 为空，分组没抽出来")

        reported_n = [g.n for g in p.groups if is_reported(g.n)]
        if reported_n and not _mentions(ev, _N_EVIDENCE_RE):
            w.append(
                f"[复核] {tag}: 填了 n={reported_n[0]!r} 却没在 evidence 里溯源——"
                f"n 是最高危字段，请确认不是脑补，否则改回 not_reported"
            )
        if is_reported(p.stats.test) and not _mentions(ev, _TEST_EVIDENCE_RE):
            w.append(
                f"[复核] {tag}: 填了 stats.test={p.stats.test!r} 却没在 evidence 里溯源——"
                f"看见星号不等于知道用了什么检验"
            )
        if not is_reported(p.stats.test) or any(not is_reported(g.n) for g in p.groups):
            has_unreported_risk = True

    if has_unreported_risk and not rec.open_questions:
        w.append(
            f"[复核] {rec.figure_id}: 存在未报告的 n 或统计方法，但 open_questions 为空——"
            f"这些正是影响可重复性的缺口，应当记下来"
        )
    return w


def validate_record(data: dict[str, Any]) -> tuple[Optional[FigureRecord], list[str]]:
    """校验模型输出，失败字段就地降级。**不抛异常。**

    分两段：
      1. 修复 —— 按 pydantic 的错误定位逐个降级（缺失补默认、类型尽量转换、
         非法枚举回退到最中性取值、烂掉的列表元素丢弃），前缀 `[修复]`
      2. 审计 —— 结构合法但可疑的地方（高危字段没溯源、schematic 却有 panel …），
         前缀 `[复核]`，不改数据

    Returns:
        `(record, warnings)`。只有 `figure_id` 缺失/为空时返回 `(None, warnings)`——
        没有标识的记录无法归档，属于不可恢复错误。
    """
    warnings: list[str] = []
    if not isinstance(data, dict):
        return None, [f"[致命] 顶层不是 JSON 对象，而是 {type(data).__name__}"]

    fid = data.get("figure_id")
    if not isinstance(fid, str) or not fid.strip():
        return None, [
            f"[致命] figure_id 缺失或为空（拿到 {fid!r}）——无法归档，请补上后重跑"
        ]

    work = copy.deepcopy(data)
    if "domain" not in work:
        # 契约里 domain 默认 mlcs，漏填时 pydantic 不会吭声——
        # 生物医学论文会被静默按 mlcs 的 profile 渲染，必须显式提醒
        warnings.append(
            f"[复核] {fid}: domain 未填 → 按契约默认值 mlcs 处理；"
            f"若是生物医学论文，profile 会整体填错，请补上或用 --profile 重抽"
        )
    if work.get("schema_version") not in (None, SCHEMA_VERSION):
        warnings.append(
            f"[复核] {fid}: schema_version={work['schema_version']!r} 与当前契约 "
            f"{SCHEMA_VERSION!r} 不一致，字段含义可能已变"
        )

    record: Optional[FigureRecord] = None
    for _ in range(_MAX_REPAIR_ROUNDS):
        try:
            record = FigureRecord.model_validate(work)
            break
        except ValidationError as exc:
            errors = exc.errors()
            fixed = False
            # 深路径优先：先删列表元素再动父级，避免索引错位
            for err in sorted(errors, key=lambda e: -len(e.get("loc", ()))):
                msg = _apply_repair(work, err)
                if msg:
                    warnings.append(msg)
                    fixed = True
            if not fixed:
                warnings.append(
                    f"[致命] {fid}: 仍有无法定位的校验错误 → "
                    + "；".join(f"{_json_path(e['loc'])}: {e['msg']}" for e in errors[:3])
                )
                return None, warnings

    if record is None:
        warnings.append(f"[致命] {fid}: 降级 {_MAX_REPAIR_ROUNDS} 轮后仍无法通过校验")
        return None, warnings

    warnings.extend(_audit(record))
    return record, warnings


# ════════════════════════════════════════════════════════════════════════════
#  汇总
# ════════════════════════════════════════════════════════════════════════════


def _natural_key(figure_id: str) -> tuple[Any, ...]:
    parts = re.split(r"(\d+)", figure_id)
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


def merge_records(out_dir: Path, *, write: bool = True) -> PaperBundle:
    """汇总 `extracted/*.json` → `figures.json`（PaperBundle）。

    顺序以 `context.json` 为准（= 论文里的原始顺序），缺失时按图号自然排序。
    每条记录都过一遍 `validate_record`，所有 warning 收进 `bundle.warnings`，
    L4 渲染与最终交付报告都从这里取「需人工复核」清单。
    """
    out_dir = Path(out_dir).resolve()
    ex_dir = out_dir / EXTRACTED_DIR
    warnings: list[str] = []

    files = sorted(ex_dir.glob("*.json")) if ex_dir.is_dir() else []
    if not files:
        warnings.append(f"[致命] {ex_dir} 下没有任何抽取结果，figures.json 将为空")

    by_id: dict[str, FigureRecord] = {}
    for f in files:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            warnings.append(f"[致命] {f.name}: 读取失败 → {exc}")
            continue

        # agent 偶尔会整份 bundle 写进来，兼容一下
        candidates = (
            payload["figures"]
            if isinstance(payload, dict) and isinstance(payload.get("figures"), list)
            else [payload]
        )
        if len(candidates) > 1 or (isinstance(payload, dict) and "figures" in payload):
            warnings.append(f"[修复] {f.name}: 写成了 bundle 而非单条记录 → 已拆开取用")

        for item in candidates:
            if isinstance(item, dict) and not str(item.get("figure_id", "")).strip():
                guessed = _figure_id_from_slug(f.stem)
                item["figure_id"] = guessed
                warnings.append(f"[修复] {f.name}: figure_id 缺失 → 按文件名补为 {guessed!r}")
            rec, warns = validate_record(item)
            warnings.extend(_tag_source(w, f.name) for w in warns)
            if rec is None:
                warnings.append(f"[致命] {f.name}: 记录不可用，已跳过")
                continue
            if rec.figure_id in by_id:
                warnings.append(f"[复核] {rec.figure_id} 出现多次，保留最后一份（{f.name}）")
            by_id[rec.figure_id] = rec

    order = _expected_order(out_dir)
    if order:
        missing = [fid for fid in order if fid not in by_id]
        if missing:
            warnings.append(
                f"[致命] 有 {len(missing)} 张图还没抽：{', '.join(missing)}——"
                f"补齐 extracted/ 后重跑 merge"
            )
        ordered = [by_id[fid] for fid in order if fid in by_id]
        ordered += [r for fid, r in by_id.items() if fid not in order]
    else:
        ordered = [by_id[k] for k in sorted(by_id, key=_natural_key)]

    bundle = PaperBundle(
        paper=_pick_paper(ordered, out_dir),
        domain=_pick_domain(ordered, warnings),
        source_pdf=_source_pdf(out_dir),
        output_dir=str(out_dir),
        figures=ordered,
        warnings=warnings,
    )
    if write:
        (out_dir / "figures.json").write_text(
            json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return bundle


def _tag_source(warning: str, filename: str) -> str:
    """把来源文件插到 `[修复]`/`[复核]` 标签后面，保持前缀可按标签过滤。"""
    m = re.match(r"^(\[[^\]]+\]\s*)(.*)$", warning, flags=re.S)
    return f"{m.group(1)}{filename} · {m.group(2)}" if m else f"{filename} · {warning}"


def _figure_id_from_slug(slug: str) -> str:
    """`figure_01` → `Figure 1`，`table_03` → `Table 3`。merge 兜底用。"""
    parts = slug.split("_")
    head = parts[0].capitalize() if parts else "Figure"
    tail = " ".join(p.lstrip("0") or "0" if p.isdigit() else p.upper() for p in parts[1:])
    return f"{head} {tail}".strip()


def _expected_order(out_dir: Path) -> list[str]:
    ctx = out_dir / "context.json"
    if not ctx.is_file():
        return []
    try:
        raw = json.loads(ctx.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [str(p.get("figure_id", "")) for p in raw if p.get("figure_id")]


def _source_pdf(out_dir: Path) -> str:
    parsed = out_dir / "parsed.json"
    if not parsed.is_file():
        return ""
    try:
        return str(json.loads(parsed.read_text(encoding="utf-8")).get("pdf_path", ""))
    except (json.JSONDecodeError, OSError):
        return ""


def _pick_paper(records: list[FigureRecord], out_dir: Path) -> PaperMeta:
    """取第一条有标题的 paper meta；都没有就退回 parsed.json 的标题。"""
    for r in records:
        if r.paper.title.strip():
            return r.paper
    return _paper_meta_from_parsed(out_dir)


def _pick_domain(records: list[FigureRecord], warnings: list[str]) -> str:
    if not records:
        return "mlcs"
    votes: dict[str, int] = {}
    for r in records:
        votes[r.domain] = votes.get(r.domain, 0) + 1
    if len(votes) > 1:
        warnings.append(
            f"[复核] 各图的 domain 判定不一致（{votes}）——"
            f"混合学科论文可用 --profile 强制统一后重抽"
        )
    return max(votes, key=lambda k: votes[k])


# ════════════════════════════════════════════════════════════════════════════
#  模式 B —— 接口预留
# ════════════════════════════════════════════════════════════════════════════


def build_api_messages(
    pack: ContextPack,
    images: list[Path],
    *,
    profile: Optional[str] = None,
    paper: Optional[PaperMeta] = None,
) -> list[dict[str, Any]]:
    """把任务文件的内容组装成 OpenAI Chat Completions 的 messages。

    模式 A/B 共用同一份提示词就靠这个函数——它复用 `_render_task()` 的正文，
    因此改提示词只需要改 EXTRACTION_RULES，两条路径同时生效。
    图片走 base64 data URL（本地切图，没有可公网访问的 URL）。
    """
    import base64
    import mimetypes

    text = _render_task(
        pack,
        images[0].parent.parent if images else Path("."),
        record_slug(pack.figure_id),
        paper or PaperMeta(),
        profile,
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for img in images:
        mime = mimetypes.guess_type(img.name)[0] or "image/png"
        b64 = base64.b64encode(img.read_bytes()).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
        )
    return [
        {
            "role": "system",
            "content": "你是文献图表实验设计抽取器。只输出一个 JSON 对象，不要任何解释文字。"
            "原文没写的字段一律填 not_reported，禁止用领域常识补全。",
        },
        {"role": "user", "content": content},
    ]


def extract_via_api(
    pack: ContextPack,
    images: list[Path],
    *,
    model: str = "gpt-4o",
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    profile: Optional[str] = None,
    paper: Optional[PaperMeta] = None,
) -> FigureRecord:
    """模式 B：调 OpenAI 兼容端点直接抽取一张图。**本期未实现。**

    留这个签名是为了让模式 A 的提示词与校验逻辑天然可复用——
    `build_api_messages()` 和 `validate_record()` 都已经就绪，
    补完只需要中间那段网络调用，约 30 行：

    TODO(模式 B) 实现步骤：
        1. `from openai import OpenAI`（本机已装）
           `client = OpenAI(base_url=base_url or os.getenv("OPENAI_BASE_URL"),
                            api_key=api_key or os.getenv("OPENAI_API_KEY"))`
        2. `messages = build_api_messages(pack, images, profile=profile, paper=paper)`
        3. `resp = client.chat.completions.create(
                model=model, messages=messages, temperature=0,
                response_format={"type": "json_object"})`
           —— 端点若不支持 json_object，改用提示词约束 + 下一步的括号截取兜底
        4. `raw = resp.choices[0].message.content`；
           用 `raw[raw.find("{"): raw.rfind("}") + 1]` 剥掉可能的 ```json 围栏后 `json.loads`
        5. `rec, warns = validate_record(payload)`；`rec is None` 就重试一次
           （把 warnings 回灌进 messages 让模型自己改），仍失败则跳过并记 warning
        6. 落盘到 `out_dir/extracted/<slug>.json`，之后照常 `merge_records()`

    注意：`temperature=0` 是硬要求。高温会直接放大 n / 剂量 / 统计检验的脑补，
    而这三项恰恰是本项目的可信度底线（PLAN.md §7 风险表最后一行）。
    """
    raise NotImplementedError(
        "模式 B（API 批量抽取）本期未实现。请走模式 A：\n"
        "  1. python scripts/pfn/extract.py tasks --out <DIR>\n"
        "  2. agent 逐个执行 tasks/*.md，写 extracted/*.json\n"
        "  3. python scripts/pfn/extract.py merge --out <DIR>\n"
        "补完模式 B 的步骤见本函数 docstring 里的 TODO。"
    )


# ════════════════════════════════════════════════════════════════════════════
#  JSON Schema 生成
# ════════════════════════════════════════════════════════════════════════════


def generate_schema(path: Optional[Path] = None, *, write: bool = True) -> dict[str, Any]:
    """从 `FigureRecord` 生成 JSON Schema 并落盘。

    契约（models.py）一变就重跑这个函数，别手改 schemas/figure.schema.json：

        python scripts/pfn/extract.py schema
    """
    base = FigureRecord.model_json_schema()
    doc = str(base.pop("description", "")).strip()
    base.pop("title", None)  # 换成下面带使用说明的版本
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://local/paper-figure-notes/schemas/figure.schema.json",
        "title": "FigureRecord",
        "description": (
            f"paper-figure-notes L3 抽取层输出：一张 figure 的结构化实验设计。{doc} "
            "本文件由 scripts/pfn/models.py 的 FigureRecord 自动生成，请勿手改——"
            "改契约请改 models.py 后重跑 `python scripts/pfn/extract.py schema`。"
            "填写规则见 SKILL.md 的两条铁律：原文没写就填 not_reported（严禁猜测 "
            "groups[].n / 剂量 / stats.test），每个字段都要在 evidence 里溯源。"
        ),
        **base,
    }
    target = Path(path) if path else DEFAULT_SCHEMA_PATH
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return schema


# ════════════════════════════════════════════════════════════════════════════
#  CLI（主 CLI 归 prep/render 用，这里只放 L3 自己的三个动作）
# ════════════════════════════════════════════════════════════════════════════


def _cli(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="pfn.extract", description="L3 抽取层：生成任务 / 汇总结果 / 重生成 schema"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tasks", help="读 context.json，生成 tasks/*.md")
    t.add_argument("--out", required=True, help="prep 的输出目录")
    t.add_argument("--profile", choices=("bio", "mlcs"), help="强制领域，跳过自动判定")
    t.add_argument(
        "--level", choices=tuple(EXTRACT_LEVELS), default=DEFAULT_LEVEL,
        help="抽取档位：light 只要「做了什么+结论」(合计约省一半) · "
             "standard 加分组与设计(约省四分之一) · full 全字段含 n/统计/溯源（默认）。"
             "读图是每张图的固定开销，档位省不掉，要按比例省请配合 --figures",
    )
    t.add_argument(
        "--figures", metavar="SPEC",
        help="只抽部分图，直接按比例省 token。"
             "`main` 只要正文图 · `ed` 只要 Extended Data · `1-4,7` 按图号 · 可逗号组合",
    )

    m = sub.add_parser("merge", help="汇总 extracted/*.json → figures.json")
    m.add_argument("--out", required=True, help="prep 的输出目录")

    s = sub.add_parser("schema", help="从 models.FigureRecord 重生成 JSON Schema")
    s.add_argument("--path", help=f"输出路径，默认 {DEFAULT_SCHEMA_PATH}")

    args = p.parse_args(argv)

    if args.cmd == "tasks":
        try:
            tasks = build_extraction_tasks(
                Path(args.out), profile=args.profile,
                level=args.level, figures=args.figures,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 2
        spec = EXTRACT_LEVELS[args.level]
        print(f"[L3] 档位 {spec['name']}（{spec['cost']}）"
              + (f" · 只抽 --figures {args.figures}" if args.figures else ""))
        print(f"[L3] 生成 {len(tasks)} 个抽取任务：")
        for t_path in tasks:
            print(f"      {t_path}")
        print("\n下一步：逐个执行上面的任务文件（Read 切图 + 按规则写 JSON），"
              f"然后 `python scripts/pfn/extract.py merge --out \"{args.out}\"`")
        return 0

    if args.cmd == "merge":
        bundle = merge_records(Path(args.out))
        fatal = [w for w in bundle.warnings if w.startswith("[致命]")]
        review = [w for w in bundle.warnings if w.startswith("[复核]")]
        fixed = [w for w in bundle.warnings if w.startswith("[修复]")]
        print(f"[L3] 汇总 {len(bundle.figures)} 图 → {Path(args.out).resolve() / 'figures.json'}")
        print(f"      致命 {len(fatal)} · 修复 {len(fixed)} · 待复核 {len(review)}")
        for w in bundle.warnings:
            print(f"      {w}")
        return 1 if fatal else 0

    if args.cmd == "schema":
        target = Path(args.path) if args.path else DEFAULT_SCHEMA_PATH
        generate_schema(target)
        print(f"[L3] schema 已生成：{target}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
