# paper-figure-notes

> 论文 PDF → 每张 figure 的**实验设计**结构化拆解 → 精读笔记 + 思维导图 + 跨文献汇总表

## 解决什么问题

读文献最耗时的不是看懂结论，是把每张图**还原成一个实验**：

> 这张图验证什么假设？分了哪几组？每组具体什么处理？测什么指标？
> 结果多少？统计怎么做的？

这些信息散在**图题 + 正文引用段 + Methods 章节**三处，人工拼接极慢，换一篇论文就要重来。
本工具把拼接自动化，输出**可跨文献横向比较**的结构化数据——写综述时可以直接筛
「所有用了 X 剂量的实验」或「所有以 Y 为 baseline 的消融」。

生物医学（`bio`）和机器学习 / 计算机（`mlcs`）两套 profile 都支持，自动判定：

| | `bio` | `mlcs` |
|---|---|---|
| 分组 | 处理组 / 对照组 | baseline / ablation / 变体 |
| n | 动物数、孔数、生物学重复 | seeds、runs |
| 处理条件 | 药物 + 剂量 + 途径 + 时长 | 模型 + 数据集 + 超参 |
| 指标 | WB / qPCR / IHC / 流式 / 生存曲线 | Accuracy / F1 / BLEU / 困惑度 |

## 最重要的一点：它不会编数据

文献整理里最致命的失败不是漏抽，是**模型把没写的 `n`、剂量、统计检验编出来**——
编出来的数字看着合理，没人会去质疑，然后混进综述。

所以整个工具围绕两条铁律设计：

1. **原文没写就填 `not_reported`，严禁猜测。** 「这类实验通常 n=3」不是证据，是脑补。
2. **每个字段可溯源。** `evidence` 记录每个字段来自图题 / 正文 / Methods / 纯读图 / 推断，
   人工复核只需要重点看 `inferred` 一栏，而不是全部重读。

推断不是不允许，但必须标出来并自带依据：柱子上有 3 个散点可以推断 n=3，
但要写成 `"3（图中散点为 3 个）"` 并把 `groups[].n` 列进 `evidence.inferred`。

`merge` 步骤还会自动查「填了 `n` 却没在 `evidence` 里溯源」这类可疑之处，标成待复核。

## 安装

```bash
pip install -r scripts/requirements.txt
# 或：pip install pymupdf "pydantic>=2" openpyxl pandas Pillow
```

Python 3.10+。挂到 Claude Code / Grok 的 skill 发现路径（Windows）：

```powershell
$repo = "<本仓库所在目录>"
cmd /c mklink /J "$env:USERPROFILE\.claude\skills\paper-figure-notes" $repo
```

macOS / Linux：

```bash
ln -s "$PWD" ~/.claude/skills/paper-figure-notes
```

挂好后直接对 agent 说「帮我精读这篇论文」或 `/paper-figure-notes` 即可，下面的命令由 agent 代跑。

**不绑定 Claude。** 四层里三层（切图、上下文装配、渲染）是纯 Python，任何环境直接跑；
只有读图那步需要多模态模型，而任务文件是**自包含 markdown**（切图绝对路径 + 原文 +
规则 + 正反例 + 输出骨架都在一个文件里），丢给任何能读图能写文件的 agent 都能执行。

Codex / Cursor / 其它 agent 的现成 prompt 见 [`docs/agent-prompt.md`](docs/agent-prompt.md)，
复制粘贴改两处路径即可。

## 用法

流水线是两段式的：确定性的部分用脚本跑，读图的部分交给多模态 agent。

```powershell
# 1. 切图 + 装配每张图的上下文
python scripts/paper_figure_notes.py prep "paper.pdf" --out notes_paper

# 2. 生成逐图抽取任务（每个任务文件自包含：切图路径 + 原文 + 规则 + 正反例 + 骨架）
python scripts/pfn/extract.py tasks --out notes_paper [--profile bio|mlcs]

# 3. ← agent 逐个执行 tasks/*.md：读图 + 读上下文 → 写 extracted/*.json

# 4. 汇总校验（非法字段自动降级，可疑之处标成待复核）
python scripts/pfn/extract.py merge --out notes_paper

# 5. 渲染：单页 index.html + 笔记 + 导图 + Excel 长表
python scripts/paper_figure_notes.py render --out notes_paper
```

### 控制耗时与成本

**时间和 token 都压在第 3 步。** 一篇 18 图论文实测：prep 约 40 秒、tasks 2 秒、
render 20 秒，而读图抽取要几十分钟。

慢的主因是**输出量**——18 张图产出约 900 KB JSON，比文本输入（529 KB 任务文件）还多，
而生成比阅读慢一个数量级。时间基本花在逐字往外写。

三个可选手段，按需要挑：

```bash
# 只要「每张图做了什么、结论是什么」，不抽分组/n/统计/溯源
python scripts/pfn/extract.py tasks --out notes_paper --level light

# 只抽正文图，跳过 Extended Data
python scripts/pfn/extract.py tasks --out notes_paper --figures main

# 组合：正文图的 1~4 张，轻量档
python scripts/pfn/extract.py tasks --out notes_paper --level light --figures main,1-4
```

第三个手段不用改命令：**一个 subagent 只抽一张图**。任务文件互不依赖，可以全部并行，
墙钟时间取决于最慢的那一张。规则会被重复读进去、输入略增，但输入便宜输出贵，这笔换算划算。

实测输入体积（以 18 图论文为基准）：`full` 全部 100% · `light` 全部 76% ·
`light --figures main` 36% · `light --figures main,1-4` 18%。
`--level` 的主要收益在**输出侧**（降到约 15~20%），而输出正是耗时主体。

**怎么选**：要做可重复性核查或跨文献汇总就用 `full`——n、统计方法、逐字段溯源缺一不可，
降档会让这些字段全空。只是想快速判断「这篇值不值得精读」，`light` 足够。
拿不准就用 `full`：省下的时间远不如漏掉 `n` 的代价大。

**不要压缩图片分辨率。** 切图约 1400×1800，压小确实能省输入，但坐标轴刻度和流式象限
百分比会读不清——省成本换来看不出的错误，不划算。

只想要单页可视化、不要其余文件：`render --outputs page`（省 1.5 秒，不省 token）。

跨文献汇总：

```powershell
python scripts/paper_figure_notes.py corpus --dirs notes_a notes_b notes_c --out corpus
```

第 3 步是唯一有不确定性的环节，被夹在中间隔离——前后都是纯函数，
改笔记模板、改导图样式都不需要重跑模型。

## 产物

```
notes_<论文名>/
  index.html                ★ 自包含单页，浏览器打开即用、可离线、可直接发人
                              五个视图：逐图精读（图钉住、panel 在旁滚）/ 全文流程 /
                              证据链导图（SVG，可缩放拖动）/ 分组检索 / 待复核
  figures/fig_01.png        高清切图（含子图标号、坐标轴文字）
  context/fig_01.md         该图的全部上下文（人可读，调试用）
  tasks/figure_01.md        抽取任务
  extracted/figure_01.json  逐图抽取结果
  figures.json              汇总，一图一条（见 schemas/figure.schema.json）
  figure-notes.md           中文精读笔记：图 + 实验设计表 + 分组结果 + 溯源标注
  figure-tables.md          四列速览：小图 / 实验方法 / 分组 / 结果
  mindmap.md / mindmap.html 整篇证据链思维导图（Mermaid / 可折叠 HTML）
  mindmaps/figure_01.md     单图分组导图，笔记里逐图引用
  groups.xlsx / groups.csv  一行一个实验组的长表
```

跨文献汇总目录另出 `all_groups.xlsx` / `.csv` 和 `corpus/mindmap.md`。

`figures.json` 里一张图长这样（节选自真实论文）：

```json
{
  "figure_id": "Figure 1",
  "domain": "bio",
  "figure_kind": "experiment",
  "question": "DMC-GF 是否在体外对胶质瘤细胞具有抗肿瘤活性，并呈剂量依赖性诱导凋亡？",
  "panels": [{
    "label": "C",
    "experiment": "凋亡率定量统计",
    "design": { "independent_var": "DMC-GF 浓度", "levels": ["0 μM", "3 μM", "5 μM"], "unit": "独立重复" },
    "groups": [
      { "name": "0 μM", "role": "control", "n": "3（图中散点为 3 个）",
        "readout": "凋亡率（%）", "result": "接近 0%", "vs_control": "基线" },
      { "name": "3 μM", "role": "treatment", "n": "3（图中散点为 3 个）",
        "readout": "凋亡率（%）", "result": "约 75%", "vs_control": "*** p<0.001" }
    ],
    "stats": { "test": "not_reported", "error_bar": "图中为误差棒 + 单点散布，类型未标注" },
    "evidence": {
      "from_caption": ["experiment"],
      "from_image_only": ["groups[].result", "stats.significance"],
      "inferred": ["groups[].n", "stats.replicates"]
    }
  }],
  "confidence": "high",
  "open_questions": ["误差棒为 SD 还是 SEM 未标注", "凋亡率统计所用检验方法未说明"]
}
```

注意 `stats.test` 是 `not_reported`——图上看得见 `***`，但原文没说用了什么检验，
所以不填。这正是这个工具想守住的东西。

## 已验证的排版

切图是整条流水线最容易翻车的一步，三种真实排版都有回归覆盖：

| 排版 | 样本 | 特点 | 曾踩的坑 |
|------|------|------|----------|
| LaTeX 双栏 | ACL 长文（11 图 19 表） | 图多为**纯矢量**绘制 | 抽内嵌图像对象会全部丢失，必须裁剪后渲染页面 |
| 期刊单栏 | *Scientific Reports*（5 图） | 每图一张大位图 | 图题常溢到下一页；`(continued)` 跨页图要合并 |
| Nature 正刊 | *Nature Genetics*（8 图 + 10 附图） | 整幅跨栏大图、图题另起一页 | 见下 |

Nature 系一次暴露四个缺陷，全部修复并写进
[`references/feasibility.md`](references/feasibility.md)：`Extended Data Fig. N` 被并进正文
`Fig. N`（应有 18 图只出 10 图）· 跨栏大图按单栏裁切（只剩八分之一还报 high 置信度）·
向上聚类被行间距截断 · 图题页无图形时降级切出正文。

最后一类最危险——**切错了还报高置信度**，下游会把残图当完整图去读。现在有覆盖率兜底：
切图占候选图形不足六成时一律不许报 `high`。

## 文档

| 文件 | 内容 |
|------|------|
| [`SKILL.md`](SKILL.md) | agent 可执行步骤 + 完整抽取规则 |
| [`PLAN.md`](PLAN.md) | 架构、数据模型、分阶段计划 |
| [`references/feasibility.md`](references/feasibility.md) | 切图方案的实测验证记录 |
| [`schemas/figure.schema.json`](schemas/figure.schema.json) | 输出契约（由 `scripts/pfn/models.py` 生成，勿手改） |
| [`docs/agent-prompt.md`](docs/agent-prompt.md) | 在 Codex / Cursor 等 agent 里使用的现成 prompt |

## 许可

[MIT](LICENSE)

## 开发

```powershell
python -m pytest tests/ -q
python scripts/pfn/extract.py schema   # 改了 models.py 后重新生成 schema
```
