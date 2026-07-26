---
name: paper-figure-notes
description: >
  把论文 PDF 里的每一张 figure 还原成一个实验：研究问题 / 分组 / 处理条件 / n /
  指标 / 结果 / 统计 / 逐字段溯源，再渲染成中文精读笔记、思维导图和跨文献汇总表。
  用于文献整理、论文精读、拆解 figure 实验设计、提取分组与对照、
  「这张图到底做了什么实验」「几篇论文的剂量和分组横向对比」、figure 转笔记。
  Use when the user asks for literature figure extraction, extracting experimental
  design or groups from paper figures, panel-level ablation/treatment tables,
  paper figure to markdown notes, or runs /paper-figure-notes.
metadata:
  short-description: "论文 figure → 实验设计结构化（分组/n/结果/溯源）→ 笔记+导图+汇总表"
---

# /paper-figure-notes — 论文 figure → 实验设计

读文献最耗时的不是看懂结论，是把每张图**还原成一个实验**：验证什么假设、分了哪几组、
每组什么处理、测什么指标、结果多少、统计怎么做。这些散在**图题 + 正文引用段 + Methods**
三处，本 skill 把拼接过程自动化，并输出可跨文献比较的结构化数据。

## 何时使用

- 用户给出论文 PDF，要求「精读 / 做笔记 / 拆实验设计 / 提取分组 / 整理成表」
- 用户问「这张图做了什么实验」「对照组是什么」「每组多少只」
- 要把多篇论文的实验条件横向对比（写综述、找剂量范围、找 baseline 设置）

**不适用**：只要摘要速览、只要翻译全文、扫描版无文本层 PDF。

## 脚本位置

本 skill 为 `D:\Tools\skills\` 下的独立项目。`{SKILL_DIR}` = 本文件所在目录
（通常 `D:\Tools\skills\paper-figure-notes`）。

```
{SKILL_DIR}/scripts/paper_figure_notes.py   # prep（切图+上下文） / render（笔记+导图+表）
{SKILL_DIR}/scripts/pfn/extract.py          # tasks（生成抽取任务） / merge（汇总校验）
{SKILL_DIR}/schemas/figure.schema.json      # 输出契约
```

依赖：Python 3.10+、`pymupdf`、`pydantic>=2`、`openpyxl`。

## 架构（为什么分四步）

```
L1 解析 ─┐
L2 装配 ─┴→ prep（确定性，可复跑）
L3 抽取  → 你（agent）读图 + 上下文，唯一有不确定性的环节
L4 渲染  → render（确定性，改样式不用重跑模型）
```

抽取质量是这个工具的**全部价值**。切图切错了能看出来，实验设计抽错了看不出来——
所以下面第 3 步的规则必须逐条执行，尤其两条铁律。

---

## 执行步骤（Agent）

### 1. 确认输入

- PDF 路径（必需）
- 输出目录：默认 `<PDF目录>/notes_<论文名>/`
- 是否强制领域 profile：`bio`（生物医学）/ `mlcs`（机器学习与计算机）。
  默认自动判定；**混合学科论文（如 AI4Science）建议让用户明确指定**，避免判错。

### 2. 跑 prep（切图 + 装配上下文）

```powershell
python "{SKILL_DIR}/scripts/paper_figure_notes.py" prep "<PDF>" --out "<DIR>"
```

可选 `--dpi 300`（密集热图 / 免疫印迹）、`--pages 1-8`（只处理部分页）。
输出 `figures/*.png`、`context/*.md`、`parsed.json`、`context.json`。
留意输出里 `⚠ 切图置信度` 的提示，这些图第 3 步要额外小心。

### 3. 生成抽取任务

```powershell
python "{SKILL_DIR}/scripts/pfn/extract.py" tasks --out "<DIR>" [--profile bio|mlcs]
```

给每张图生成 `tasks/<figure_id>.md`，里面已经打包好：切图绝对路径、图题原文、
正文引用段（带 `§` 段号）、Methods 片段、表格文本网格、**完整填写规则、正反例、
预填好的输出骨架**。任务文件是自包含的——可以整个丢给 subagent 并行执行。

### 4. 逐图抽取（核心）

对每个 `tasks/*.md`：**Read 任务文件 → Read 里面列出的切图 PNG → 写
`extracted/<figure_id>.json`**（一图一条 `FigureRecord`，不是 bundle）。

必须真的看图。上下文里没有的数值——坐标轴刻度、图例、误差棒、显著性星号、
流式象限百分比、IC50、散点个数——只能从图上读，这是本工具相对「只读文字」的全部增量。

#### 铁律 1 · 原文没写就填 `not_reported`，严禁猜测

最高危三项：**`groups[].n`**、**`condition` 里的剂量 / 超参数值**、**`stats.test`**。
模型最爱在这三处脑补，一旦编错整张汇总表就不可信了。

写下任何具体数值前，先回答「它出现在哪？」——只有四个合法答案：
① 图题的第几句 ② 正文哪一段（`§2.3¶1`）③ Methods 哪一节（`§4.2`）④ 图上什么位置。
**答不出来 → 一律 `not_reported`。**

明令禁止的推理：「这类实验通常 n=3」「多组比较通常用 ANOVA」「误差棒一般是 SD」
「ML 论文一般跑 3 个 seed」。**领域常识不是本文的数据。** 别的 panel 写了 n=3 也不能顺手抄。

任务文件里的 **Methods 片段是关键词召回来的，未必对应本图的实验**。引用前先确认它讲的
就是这张图做的事；只是术语撞上了就别用——张冠李戴的剂量比空白更糟。

`not_reported` 是合格答案，不是失败。空白会被复核补上；一个看起来合理的假数字
不会被任何人质疑，危害大得多。

**描述你看见的事实，而不是它的名字**：图上有误差棒但没标类型 →
`"图中有误差棒，类型未标注"`，不要写 `"SD"`。

#### 反过来的错误：图题写了却填 `not_reported`

铁律 1 防「无中生有」，这一条防「视而不见」——**两者一样是错的**。

任务文件里的**图题总述**（第一个 `(A)` 之前）和**图题收尾**（最后一个子图描述之后）
讲的是**整张图**，对 **A~Z 每一个 panel 都成立**。而最高价值的信息恰恰常年藏在收尾里：

```
* indicates p < 0.05, ** p < 0.01, *** p < 0.001. N = 3.
The control group was used for comparison. Data are expressed as mean ± SD.
```

这一段同时交代了高危三项里的两项。**它不属于最后那个子图**——只算给 panel D，
A/B/C 就会白白填成 `not_reported`。

所以：把任何 panel 的 `n` / `stats.error_bar` / `stats.significance` 填成 `not_reported`
之前，**先回头确认图题总述与收尾里没写 `N = ...`、`mean ± SD`、`* p<0.05`**。
有就逐个 panel 都填上，`evidence` 记 `from_caption`（原文明示，不进 `inferred`）。

#### 铁律 2 · 每个字段必须可溯源

`evidence` 五个桶装字段路径（`groups[].n`、`stats.significance`）或短标签（`IC50 数值`）：

| 桶 | 装什么 |
|----|--------|
| `from_caption` | 图题写明的 |
| `from_body` | 正文给出的，**写上段号** `§2.3¶1` |
| `from_methods` | Methods 给出的，写节号 `§4.2` |
| `from_image_only` | 只能读图得到的：图上印的数值、柱高、象限百分比、星号 |
| `inferred` | **模型推断的** ← 人工复核只看这一栏 |

**只要一个值不是原文逐字给出，就必须进 `inferred`**，宁滥勿缺。
推断值还要**自带依据**：`"n": "3（图中散点为 3 个）"` ✅，`"n": "3"` ❌。

数值照抄原样，不换算不四舍五入。图上印着 `86.63%` 就写 `86.63%`；
只能目测柱高就写 `约 75%`，不要写 `75.0%` 假装精确。

#### ❌ 反例（同一张图的错误抽法，四处错误各自致命）

```json
{ "groups": [{ "name": "3 μM", "n": "3" }],
  "stats": { "test": "t-test", "error_bar": "SD" },
  "evidence": { "inferred": [] } }
```

1. `n: "3"` — 数字也许对，但看不出依据，复核无从查起
2. `test: "t-test"` — 原文根本没说，是「两组比较应该用 t 检验」的脑补
3. `error_bar: "SD"` — 图上只有误差棒没标类型，编了个最常见答案
4. `inferred: []` — 上面全是推断却声称零推断。**最严重**：假数据以「原文明示」的
   身份进综述，复核会直接跳过

#### 领域 profile（先判定 `domain`，再按对应列填）

| | `bio` | `mlcs` |
|---|---|---|
| 识别信号 | 细胞株、动物、μM / mg·kg⁻¹、WB / qPCR / 流式 / 生存曲线 | 数据集、baseline、ablation、超参、Accuracy / F1 / BLEU |
| 分组 = | 处理组 / 对照组 | baseline / ablation / 变体 |
| `n` = | 动物数、孔数、生物学重复 | seeds、runs |
| `condition` = | 药物 + 剂量 + 途径 + 时长 | 模型 + 数据集 + 超参 |
| `readout` = | WB / qPCR / IHC / 流式 / 生存曲线 | Accuracy / F1 / BLEU / 困惑度 |

交叉学科按**这张图本身测什么**判定，不按论文整体基调。有 `--profile` 就直接用。

#### panel 是分析单位

一张图常含多个独立实验，**必须拆开**：按图题的 `(A)(B)(C)` 和图上标号逐个建 panel。
同一 panel 里多个细胞株 / 数据集若共用自变量 → 作为 `groups` 的不同行
（组名写成 `U251 · 12 h`）；自变量本身不同 → 拆两个 panel。整图不分子图 → 一个 panel，
`label` 填 `"-"`。

`figure_kind == "schematic"`（机制示意图 / 架构图）**不含实验，`panels` 留空 `[]`**，
只写 `question` 和 `figure_conclusion`。

#### `role` 怎么选（跨文献长表按它筛）

`control` 溶剂 / 空载 / 未处理对照 · `treatment` 受试处理 · `baseline` 对比的已有方法 ·
`ablation` 从完整方法里拿掉一个组件 · `reference` 只作参照不进主结论（阳性药、oracle 上限）。
对照组自己的 `vs_control` 统一写 `"基线"`。

#### 命名与语言

叙述性字段（`question` / `experiment` / `finding` / `figure_conclusion`）写中文；
**标识性字段保留原文**：组名照抄图例或横轴、基因名、药名、细胞株、模型名、数据集名、
指标名（翻译了就没法跨文献比对）。

#### 提交前自检（逐条回答，答不上来就改回 `not_reported`）

1. 每个非 `not_reported` 的 **`n`**，能说出出自哪句图题、哪个 `§` 段、还是图上哪个位置吗？
2. **`stats.test`** 若已填，原文里**逐字出现过**这个检验名吗？（看见星号 ≠ 知道用了什么检验）
3. `condition` 里的剂量 / 时长 / 超参，都能指认出处吗？
4. 所有非原文逐字给出的值，都进 `inferred` 了吗？
5. 组名照抄了图例 / 横轴原文，没翻译、没自己起名吗？
6. **图题总述与收尾**里的 `N = ...`、`mean ± SD`、`* p<0.05`，
   是否已填进**每一个** panel 的 `n` / `stats`？（漏填和编造一样是错的）

### 5. 汇总校验

```powershell
python "{SKILL_DIR}/scripts/pfn/extract.py" merge --out "<DIR>"
```

合并 `extracted/*.json` → `figures.json`，并逐条校验：

- `[修复]` 字段不合法已自动降级（非法枚举 → 最中性取值，类型错 → 尽量转换，缺失 → `not_reported`）
- `[复核]` 结构合法但可疑，两个方向都查：
  - **多填**：填了 `n` / `stats.test` 却没在 `evidence` 里溯源（脑补现场）
  - **漏填**：**图题已报告却填成 `not_reported`**（图题里出现 `N = 3`、`mean ± SD`、
    `p<0.05`，panel 却说没有）
  - 另有 `schematic` 却带 panel、`evidence` 全空、`domain` 未填
- `[致命]` 有图没抽、文件读不了 → 补齐后重跑

**`[复核]` 里关于 n 和 stats 的每一条都要回去看**——它们正是两条铁律的自动哨兵。
哨兵是**独立判断**，不看记录自报的 `inferred` / `confidence`：脑补的记录往往正好
谎称 `inferred: []` + `confidence: high`，只有哨兵能抓住它。

### 6. 渲染

```powershell
python "{SKILL_DIR}/scripts/paper_figure_notes.py" render --out "<DIR>" --mindmap-depth group
```

`--mindmap-depth {figure|panel|group|result}`（默认 `group`；10 图论文展开到 `result`
会有上百节点，糊成一团）。`--mindmap-format mermaid markmap opml`。

跨文献汇总：

```powershell
python "{SKILL_DIR}/scripts/paper_figure_notes.py" corpus --dirs "<DIR1>" "<DIR2>" --out "<CORPUS>"
```

### 7. 交付

向用户报告：

0. **先给 `index.html`**——上千行的 markdown 和几百行的 xlsx 都不是「能拿起来直接看」的东西。
   单页把切图、分组表、溯源与复核清单聚在一处，是默认的交付入口；其余文件按需再提。
1. 其余产物路径（`figure-notes.md` / `figure-tables.md` / `mindmaps/` / `groups.xlsx`）
2. **需人工复核的字段清单**——把 `figures.json` 的 `warnings` 里所有 `[复核]`
   和各图 `evidence.inferred` 非空的 panel 列出来，注明「这些是模型推断，不是原文明示」
3. `confidence: low` 的图，以及切图可能不完整的图
4. `open_questions` 汇总（这篇论文哪里交代不清）

不要只说「已完成」——**明确告诉用户哪里可信、哪里要自己再看一眼**。

---

## 输出结构

```
notes_<论文名>/
  index.html           # ★ 交付首选：自包含单页，双击用浏览器打开，可离线、可直接发人
                       #   三个视图：逐图精读 / 分组检索 / 待复核
  parsed.json          # L1：章节树 + 段落 + 图资产
  context.json         # L2：每图的上下文包
  context/fig_XX.md    # 同上，人可读（调试模型为何抽错时看这个）
  figures/fig_01.png   # 切图
  tasks/figure_01.md   # L3 任务（自包含，可丢给 subagent）
  extracted/figure_01.json   # L3 逐图输出
  figures.json         # L3 汇总（PaperBundle，render 的唯一输入）
  figure-notes.md      # L4 精读笔记
  mindmap.md / .html   # L4 整篇证据链导图（Mermaid / Markmap）
  mindmaps/figure_01.md      # L4 单图分组导图（笔记里引用）
  groups.xlsx / groups.csv   # L4 一行一个实验组的长表
```

跨文献汇总目录（`corpus` 子命令）另出 `all_groups.xlsx` / `.csv` 与 `corpus/mindmap.md`。

## 故障排查

| 现象 | 处理 |
|------|------|
| `该 PDF 没有文本层` | 扫描版，先 OCR；不要硬跑 |
| 切图置信度 `low` / 图被截断 | 提高 `--dpi`；实在不行在 `open_questions` 注明并把 `confidence` 降到 `low` |
| `找不到 context.json` | 先跑 `prep`；`tasks` 依赖它 |
| 重跑 `prep` 后 agent 说找不到任务文件 | `prep` 会重建整个输出目录，`tasks/` 与 `extracted/` 都会没。**重跑 prep 后必须重新跑一次 `tasks`**；已抽好的 `extracted/` 想保留就先拷出去 |
| 切图变了但已抽过 | 基于旧切图的 `extracted/*.json` 全部作废，必须重抽——残图抽出来的实验设计会缺子图且看不出来 |
| merge 报「还没抽」 | `extracted/` 缺文件，对照 `tasks/` 补齐 |
| merge 报 `domain 判定不一致` | 混合学科，用 `--profile` 强制统一后重抽 |
| merge 报「填了 n 却没溯源」 | 回去看那张图：要么补 `evidence.inferred`，要么改回 `not_reported` |
| 表格数字读错小数点 | 以任务文件「表格文本网格」一节为准，别信目测 |
| merge 报「图题已报告却填成 not_reported」 | 图题的总述 / 收尾适用于全图，回去把 `N`、`mean ± SD`、`p<0.05` 填进**每个** panel |
| 导图节点糊成一团 | 降 `--mindmap-depth` 到 `panel` 或 `figure` |
| 契约改了 schema 没跟上 | `python "{SKILL_DIR}/scripts/pfn/extract.py" schema` 重新生成 |

## 设计要点（勿改坏）

1. **两条铁律是这个工具的可信度底线**。宁可满屏 `not_reported`，也不要一个编出来的
   `n=3`——空白会被复核补上，假数字不会被任何人质疑。
2. **提示词的唯一真相源是 `scripts/pfn/extract.py` 的 `EXTRACTION_RULES` / `FEW_SHOT` /
   `SELF_CHECK`**。本文件是它的常驻副本，`tests/test_extract.py` 做漂移检测。改规则先改
   extract.py，两条路径（agent 模式与将来的 API 模式）才会同时生效。
3. **panel 是分析单位**，不是图。一张图拆不开，跨文献汇总表就没有可比的行粒度。
4. **`evidence.inferred` 决定人工复核的成本**。漏标一条，复核就得整篇重读。
5. **L1/L2/L4 确定性，L3 隔离在中间**。改导图样式、改笔记模板都不用重跑模型。
6. **Windows 编码**：所有文本写盘必须 `encoding="utf-8"`。
7. **`schemas/figure.schema.json` 不要手改**，它由 `models.FigureRecord` 生成。

设计背景见 [`PLAN.md`](PLAN.md)，切图方案的实测证据见
[`references/feasibility.md`](references/feasibility.md)。
