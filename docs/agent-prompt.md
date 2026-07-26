# 在 Codex / Cursor / 其它 agent 里使用

本工具**不绑定 Claude**。四层里三层（切图、上下文装配、渲染）是纯 Python，
任何环境直接跑；只有读图那一步需要多模态模型。

关键在于 `tasks/*.md` 是**自包含**的——切图的绝对路径、图题原文、正文引用段、
Methods 片段、填写规则、正反例、输出骨架全打包在一个文件里。
所以任何「能读图片、能写文件、能跑命令」的 agent 都能执行，不需要本仓库的其它上下文。

---

## 一、直接粘贴给 agent 的 prompt

把下面整段发给 Codex / Cursor / 其它 agent，**只需把两处尖括号替换掉**：

````text
你要用 paper-figure-notes 这个工具把一篇论文 PDF 里的每张 figure 拆解成结构化的实验设计。

仓库：<本仓库的本地路径，例如 D:\repos\paper-figure-notes>
论文：<论文 PDF 的绝对路径>

依赖：Python 3.10+，`pip install -r scripts/requirements.txt`

## 步骤

1) 切图 + 装配上下文（纯 Python，无需模型）

   python scripts/paper_figure_notes.py prep "<PDF>" --out "<输出目录>"

   留意输出里的「⚠ 切图置信度」提示，这些图第 3 步要额外确认完整性。

2) 生成逐图抽取任务

   python scripts/pfn/extract.py tasks --out "<输出目录>"

   可选降本：`--level light` 只要「做了什么+结论」；`--figures main` 只抽正文图。
   两个开关可组合，如 `--level light --figures main,1-4`。

3) 逐图抽取 —— 这一步由你来做，是全流程唯一需要模型的环节

   对 `<输出目录>/tasks/` 下的每个 .md 文件：
     a. 完整读一遍任务文件（里面有全部规则、正反例和输出骨架）
     b. **打开它列出的切图 PNG 并真的看图**
     c. 按骨架写 JSON 到 `<输出目录>/extracted/<同名>.json`

   下面两条铁律是这个工具的全部价值所在，必须逐条执行：

   【铁律一】原文没写就填 "not_reported"，严禁猜测。
     最高危的三项是 groups[].n、condition 里的剂量/超参、stats.test。
     写下任何具体数值前先回答「它出现在哪」——只有四个合法答案：
     图题的第几句 / 正文哪个 § 段 / Methods 哪一节 / 图上什么位置。
     答不出来一律 not_reported。

     明令禁止这类推理：「这类实验通常 n=3」「多组比较通常用 ANOVA」
     「误差棒一般是 SD」「ML 论文一般跑 3 个 seed」。领域常识不是本文的数据。
     别的 panel 写了 n=3 也不能顺手抄给这个 panel。

     反过来也是错的：图题的总述与收尾（常含 "N = 3"、"mean ± SD"、"* P < 0.05"
     的定义）**适用于全图每一个 panel**，填 not_reported 前先回头看那两段，
     别把图题白纸黑字写了的东西丢掉。

   【铁律二】每个字段必须可溯源，填进 evidence 的五个桶：
     from_caption / from_body（带 § 段号）/ from_methods（带节号）/
     from_image_only（只能读图得到的：坐标轴、图例、误差棒、显著性星号、
     流式象限百分比、散点个数）/ inferred（模型推断的）。

     只要一个值不是原文逐字给出，就必须进 inferred，宁滥勿缺。
     推断值要自带依据：
       "n": "3（图中散点为 3 个）"   ✅
       "n": "3"                      ❌ 看不出这个 3 从哪来

     数值照抄原样，不换算不四舍五入。图上印着 86.63% 就写 86.63%；
     只能目测柱高就写「约 75%」，不要写 75.0% 假装精确。

   其它要点（任务文件里有完整版）：
     · panel 是分析单位，一张图常含多个独立实验，必须按 (a)(b)(c) 拆开
     · figure_kind 是 schematic（机制示意图/架构图）时 panels 留空数组 []
     · 组名照抄图例或横轴原文，不要翻译（跨文献比对要靠它对齐）
     · 叙述性字段（question / experiment / finding / figure_conclusion）写中文
     · 任务文件若标了切图置信度 medium/low，先确认子图标号是否齐全，
       缺了就写进 open_questions 并把 confidence 降到 low

4) 汇总校验

   python scripts/pfn/extract.py merge --out "<输出目录>"

   会逐条查「填了 n 却没溯源」「填了 stats.test 却没溯源」这类可疑之处，
   标成 [复核]。这些不是错误，是需要人判断的地方。

5) 渲染

   python scripts/paper_figure_notes.py render --out "<输出目录>"

   产出 index.html（自包含单页，浏览器打开即用）、精读笔记、思维导图、
   一行一个实验组的 Excel 长表。只要单页可加 `--outputs page`。

## 交付

先给 index.html 的路径——上千行的 markdown 不是能拿起来直接看的东西。
然后报告：抽了多少 panel / 多少实验组、哪些字段进了 inferred（人工复核只需看这些）、
merge 标出的 [复核] 条目、以及 confidence 为 low 的图。

不要只说「已完成」，要明确讲清哪里可信、哪里需要自己再看一眼。
````

---

## 二、并行加速（可选）

18 张图的论文，逐张串行读图较慢。若你的 agent 支持并行子任务，
可以把 `tasks/` 按图分组丢给多个子 agent——任务文件自包含，互不依赖，
唯一约束是每个子 agent 只写自己那几个 `extracted/*.json`，
全部完成后再由主 agent 统一跑 `merge` 和 `render`。

实测：18 张图分给 6 个子 agent（每人 3 张）比串行快数倍。

## 三、成本

token **全部**花在第 3 步读图。第 1、2、5 步是纯 Python，一篇 18 图的论文
加起来约 2 秒、零 token。所以要省钱：

- `--level light`：只要「做了什么+结论」，输入降到 76%、输出降到约 15~20%
- `--figures main`：只抽正文图，按图数比例省

读图是每张图的固定开销，档位省不掉——要按比例省只能靠 `--figures`。

## 四、不需要 agent 的用法

若只想要切图和上下文（比如自己看图、或喂给别的流程），跑完第 1 步即可：
`figures/*.png` 是高清切图，`context/*.md` 是每张图的全部相关原文，都能独立使用。
