# paper-figure-notes — 实施计划

> 文献 PDF → 每张 figure 的**实验设计**结构化拆解（细到分组与结果）→ 笔记 + 思维导图 + 跨文献汇总表

创建于 2026-07-26。技术可行性已用真实论文验证，见 [`references/feasibility.md`](references/feasibility.md)。

---

## 0. 实施进度（2026-07-26）

P0~P4 已实现并通过端到端验收。五层由并行开发，靠 `scripts/pfn/models.py` 的数据契约对接，
`tests/test_integration.py` 第一段做契约一致性检查（20/20 通过）。

| 阶段 | 状态 | 验收结果 |
|------|------|----------|
| P0 骨架 | ✅ | 两段式 CLI（`prep` / `render` / `corpus`） |
| P1 解析层 | ✅ | ACL 11 图 19 表、SciRep 5 图全部正确切出；跨页图合并、图题跨页配对均生效 |
| P2 装配层 | ✅ | 交叉引用与 Methods 召回准确；panel 切分修复中（见下） |
| P3 抽取层 | ✅ | 任务文件自包含；防幻觉哨兵实测有效（见下） |
| P4 渲染层 | ✅ | 笔记 / 三格式导图 / xlsx 长表均产出 |
| P5 打磨 | ⬜ | 缓存、断点续跑、paper-search 打通、补充材料 |

### 端到端验收怎么做的

在 Scientific Reports 那篇上完整走了一遍真实流程：`prep` → 人工按 SKILL.md 读图抽取
Figure 5（原位胶质瘤 TMZ 联合给药）→ `merge` → `render`。规则可执行，产出可用。

**防幻觉机制经过了对抗性验证。** 故意造了一条符合 SKILL.md「反例」的记录：
`n="3"` 无依据、`stats.test="t-test"` 原文没说、`error_bar="SD"` 图上没标，
且 `evidence.inferred: []` 谎称零推断、`confidence: "high"`。
merge 的两个哨兵准确命中，同时**没有**误伤诚实抽取里那条带依据、已标 `inferred` 的 `n`。
这条边界正是工具可信度的核心。

### 验收暴露并已修复的三个问题

1. **自报可信度不可信**（最重要）。脑补的记录往往正好谎报 `inferred: []` + `confidence: high`，
   于是在速览表和导图里显示得**比诚实记录还干净**。修法：契约新增 `review_reasons()`，
   把 merge 哨兵的独立判断并入 ⚠ 判定，不再单凭自报。
   ✅ 已验证：造假记录现在在速览表显示 `高 ⚠` + `⚠ 哨兵`，导图节点也带 ⚠。
2. **图题的全图共享文字被吞进末尾 panel**。`N = 3`、`mean ± SD`、`* p<0.05` 的定义常写在
   图题结尾，适用于所有 panel。被吞后前面几个 panel 会把图题已报告的 `n` 误填成
   `not_reported`——**图题白纸黑字写了的数据被我们自己弄丢**。
   修法：契约新增 `caption_preamble` / `caption_trailer`。
   ✅ 已验证：SciRep 五张图的 `N = 3` 现均归入全图级，对每个 panel 可见。
3. **panel 切分对无句末标点的图题失效**（`...growth (B) Measure...`）。
   ✅ 已修复：SciRep 5/5 正确（Figure 4 → A B C D，Figure 5 → A B）。

### 复核理由为什么是结构化的

`review_reasons()` 返回 `dict[str, list[ReviewReason]]` 而不是拼好的字符串列表，
由两个渲染层在集成后联名提出，理由都成立：

1. **panel 归属不能在源头丢。** 哨兵警告形如
   `[复核] … Figure 3 panels[2](C): 填了 n='3' 却没溯源`，panel 标记位于第一个冒号
   **之前**。早期实现只保留冒号之后的正文，多 panel 图便无法定位该核哪个 panel。
   信息在解析时被销毁，下游再怎么努力也取不回来。
2. **渲染层不该靠文案前缀分流。** 用 `startswith("校验哨兵")` 区分来源，
   意味着改一次文案，两个下游都静默退化成普通 ⚠ 而不报错。改用 `kind` 枚举后，
   文案是纯展示物。

`inferred` 那路也随之改成**逐 panel 一条**并携带真实字段路径，三路理由形状统一，
渲染层可走同一套代码。

### 一条方法论教训

判断 panel 结构时，**图题文法不是真相，图本身才是**。
我一度凭 `Projection (a) shows...` 的句法断定 ACL Figure 1 不该切分，
回去看切图才发现那张图确实有 `(a)` `(b)` 两个虚线框子图。
好在 L3 会读图，轻微过切可由它合并，而漏切则少了上下文——所以**边界上宁可倾向切分**。

### 已知的次要边界

ACL Figure 1 目前不切分（图实际有两个子图）；Figure 7 切出的描述丢了主语。
不阻塞可用性：L3 同时拿得到完整图题原文与图像，能自行判定 panel 结构。

---

## 1. 要解决的问题

读文献时最耗时的不是看懂结论，而是把每张图**还原成一个实验**：
这张图在验证什么假设？分了哪几组？每组的具体处理条件是什么？测的什么指标？
结果的方向和数值是多少？统计怎么做的？——这些信息散落在
**图题 + 正文引用段 + Methods 章节**三处，人工拼接极慢，且换一篇论文就要重来一遍。

本工具把这个拼接过程自动化，并输出**可跨文献横向比较**的结构化数据。

### 交付物

| 产物 | 说明 |
|------|------|
| `figures/fig_XX.png` | 每张图的高清切图（含子图标号、坐标轴文字） |
| `figures.json` | 结构化实验设计（§4 schema），一图一条 |
| `figure-notes.md` | 中文精读笔记：图 + 速览表 + 单图导图 + 各 panel 详细分组，可直接进 Obsidian |
| `figure-tables.md` | **四列速览表**：一张图一表，`小图 / 实验方法 / 分组 / 结果`，一屏看完一张图 |
| `mindmap.md` / `.html` / `.opml` | 全篇证据链导图（§5） |
| `mindmaps/figure_XX.md` / `.html` | **一张图一个导图**：图题 → 各小图 → 自变量 → 分组 → 结果（§5 层级②） |
| `groups.xlsx` / `.csv` | **一行一个实验组**的长表（33 列），跨文献筛选比较用 |

### 已确认的决策

| 决策 | 选择 | 影响 |
|------|------|------|
| 文献领域 | **两个都要，auto 判定** | 内置 `bio` / `mlcs` 两套 profile，模型先判定领域再选；P1 需用两种排版各复测一次 |
| 抽取引擎 | **先做 Agent，留 API 接口** | P3 只实现 agent 模式跑通质量；schema 与提示词设计成可直接复用，`--api` 开关留到确需批量时再补 |

---

## 2. 项目定位与目录

沿用 `D:\Tools\skills\` 的独立项目约定（与 `video-to-notes` 一致）：

```
D:\Tools\skills\paper-figure-notes\
  SKILL.md            # Agent 可执行步骤（待 P0 写）
  README.md           # 人看的说明（待 P0 写）
  PLAN.md             # 本文件
  scripts\            # Python CLI
  schemas\            # JSON Schema 定义
  references\
    feasibility.md    # ✅ 已完成：技术验证记录
```

挂载到发现路径（Claude Code 用 `.claude\skills`，Grok 用 `.grok\skills`）：

```powershell
cmd /c mklink /J "C:\Users\zzy\.claude\skills\paper-figure-notes" "D:\Tools\skills\paper-figure-notes"
```

业务数据（下载的 PDF、生成的笔记）不放这里，默认落在 `<PDF所在目录>\notes_<论文名>\`。

---

## 3. 架构：三层，确定性与智能严格分离

```
┌─ L1 解析层（纯 Python，确定性，可复现） ─────────────────┐
│  PDF → 切图 PNG + 结构化正文 + caption + 章节树         │
│  不做任何理解，只做几何与文本提取                        │
└──────────────────────────────────────────────────────────┘
                          ↓ context pack（每图一个）
┌─ L2 装配层（纯 Python） ─────────────────────────────────┐
│  为每张图凑齐它需要的全部上下文：                        │
│  图题 + 所有引用该图的正文段 + 相关 Methods 片段         │
└──────────────────────────────────────────────────────────┘
                          ↓
┌─ L3 抽取层（多模态模型） ────────────────────────────────┐
│  看图 + 读上下文 → 填 §4 的结构化 schema                 │
│  模式 A：Claude Code agent 直读 PNG（本期实现）          │
│  模式 B：脚本调 OpenAI 兼容端点批量跑（接口预留）        │
└──────────────────────────────────────────────────────────┘
                          ↓
┌─ L4 渲染层（纯 Python，无模型） ─────────────────────────┐
│  figures.json → 笔记 MD / 思维导图 / Excel 长表          │
│  纯函数，可脱离前面三层单独重跑                          │
└──────────────────────────────────────────────────────────┘
```

**为什么这样分层**：L1/L2/L4 是确定性的，出错可定位可复跑；L3 是唯一有不确定性的环节，
被隔离在中间，输入完全由 L1/L2 固定、输出完全喂给 L4，便于换模型、加缓存、做回归对比。
思维导图放在 L4，意味着**改导图样式不需要重新调模型**。

### L1 关键实现（两种排版均已实测通过，细节见 feasibility.md）

**核心是排版自适应分派**——一条规则同时覆盖两个领域：

```
若 页面存在单个位图且面积 ≥ 10% 页面：
    → 期刊排版路线：直接取 image rect 作 bbox      （Nature/MDPI/Frontiers 系，生物医学）
否则：
    → LaTeX 排版路线：矢量聚类 + 分栏 + ceiling 规则（ACL/NeurIPS/arXiv 系）
```

- 切图统一走「算 bbox → `get_pixmap(clip=...)` 渲染」，**绝不抽内嵌图像对象**
  （ACL 样本 11 张图里 7 张是纯矢量，抽 XObject 会全部丢失）
- **caption 与图的配对独立于切割**：同页就近 → 上一页尾部 → 下一页顶部，三级回退 + 图号校验
  （Nature 系图题常溢出到下一页，实测 Fig 2 图在 p6、题在 p7）
- **跨页图合并**：`Fig. N (continued)` 按图号归入同一 figure，输出多张切图而非拼接成巨图
  （VLM 分开读更准）
- 表格走图像 + 文本网格双通道，不用 `find_tables()`（三线表上已证伪）
- 全链路降级兜底：图形候选空 → 切整栏带 → 切整页 + 标记低置信

### L2 关键实现

- **正文交叉引用**：正则扫 `Fig(ure)?\.?\s*\d+[A-Za-z]?` / `图\s*\d+`，
  把命中句子所在的**整段**收进该图的 context，并记录 `§章节号`
- **Methods 关联**：从图题和引用段抽实验方法关键词（细胞系/动物品系/试剂/数据集/模型名/指标名），
  回到 Methods 或 Experimental Setup 章节做关键词召回，取 top-k 段
- **子图切分**：caption 里的 `(A)` `(a)` `A.` 分段解析出各 panel 的描述，与图像 panel 对齐

---

## 4. 数据模型（核心设计）

一张图一条记录。**panel 是分析单位**，因为一张图常含多个独立实验。

```jsonc
{
  "paper": { "title": "...", "year": 2025, "venue": "ACL", "doi": "..." },
  "domain": "bio | mlcs",          // auto 判定结果，决定下方字段的填法
  "figure_id": "Figure 3",
  "page": 4,
  "image": "figures/fig_03.png",
  "caption": "<图题原文>",
  "figure_kind": "experiment | schematic | qualitative_example | analysis",
  //  schematic（框架示意图）不含实验，标记后跳过分组抽取

  "question": "这张图要回答的问题 / 验证的假设",

  "panels": [{
    "label": "A",
    "experiment": "做了什么实验（一句话）",

    "design": {
      "independent_var": "被操纵的因素（= 分组依据）",
      "levels": ["水平1", "水平2", "水平3"],
      "controlled": ["保持固定的条件"],
      "unit": "实验单位：动物 / 细胞孔 / 样本 / 模型 / 数据集"
    },

    "groups": [{
      "name": "组名（按图中图例/横轴标签原文）",
      "role": "treatment | control | baseline | ablation | reference",
      "condition": "具体处理：药物+剂量+途径+时长 / 超参+数据集+模型规模",
      "n": "样本量或重复数（未写则 not_reported）",
      "readout": "测量指标 + 方法（WB / qPCR / 流式 / Accuracy / BLEU …）",
      "result": "数值 + 方向",
      "vs_control": "相对对照组的变化（倍数 / 百分点 / 显著性）"
    }],

    "stats": {
      "test": "t-test / ANOVA+post hoc / 未说明",
      "error_bar": "SD | SEM | CI | 未说明",
      "replicates": "生物学重复 / 技术重复 / random seeds",
      "significance": "* p<0.05 等标注含义"
    },

    "finding": "该 panel 的结论",

    "evidence": {
      "from_caption": ["哪些字段来自图题"],
      "from_body": ["§3.2 ¶2"],
      "from_methods": ["§2.1"],
      "from_image_only": ["只能靠读图得到的字段"],
      "inferred": ["模型推断而非原文明示的字段 ← 人工复核重点"]
    }
  }],

  "figure_conclusion": "整图结论",
  "confidence": "high | medium | low",
  "open_questions": ["原文未交代、影响可重复性的点"]
}
```

### 两条铁律（防幻觉）

1. **原文没写就写 `not_reported`，不许猜。** 尤其是 `n`、剂量、统计检验——
   这三项恰恰是文献整理里最容易被模型脑补、也最致命的。
2. **每个字段必须可溯源。** `evidence` 记录信息来自图题 / 正文 / Methods / 纯读图 / 推断。
   整理综述时只需重点复核 `inferred` 列表里的字段，而不是全部重读。

### 领域 profile（auto 判定，两套都实现）

同一套骨架，`design` / `groups` 的填法按领域切换：

| profile | 分组 = | n = | condition = | readout = |
|---------|--------|-----|-------------|-----------|
| `bio` | 处理组 / 对照组 | 动物数、孔数、重复数 | 药物+剂量+途径+时长 | WB / qPCR / IHC / 流式 / 生存曲线 |
| `mlcs` | baseline / ablation / 变体 | seeds、runs | 模型+数据集+超参 | Accuracy / F1 / BLEU / 困惑度 |

判定方式：L3 抽取时先看图题 + 摘要给出 `domain`，再按对应 profile 填写。
`--profile bio|mlcs` 可强制覆盖，避免混合学科论文判错。

---

## 5. 思维导图

`figures.json` 本身就是一棵树，导图是它的**纯渲染**，无需额外模型调用，改样式可单独重跑。

### 三个层级

**① 单篇证据链导图（`mindmap.md`，最常用）**
一眼看出这篇论文用几张图、几个实验、支撑了哪条逻辑链：

```
论文核心主张
├── Figure 1 · 问题A
│   ├── Panel A · 实验X
│   │   ├── 对照组 → 结果
│   │   └── 处理组 → 结果
│   └── ✅ 结论
├── Figure 2 · 问题B
└── ...
```

**② 单图分组结构导图**（嵌在笔记里每张图下方）
Figure → panel → 自变量 → 各水平（= 分组）→ 结果。用于快速回忆某张图的设计。

**③ 跨文献主题导图**（`corpus/mindmap.md`）
主题 → 子问题 → 各论文的证据（谁用什么模型/剂量、得到什么结论）。写综述时用。

### 输出格式

| 格式 | 用途 | 依赖 |
|------|------|------|
| **Mermaid `mindmap`**（默认，内嵌 `.md`） | Obsidian / Typora / Artifact 直接渲染 | 零依赖 |
| **Markmap 单文件 HTML** | 可折叠、可缩放，图大时浏览 | 内联 JS，自包含 |
| **OPML / FreeMind `.mm`** | 导入 XMind / 幕布继续手工编辑 | 纯 XML 拼接 |

### 实现注意

- **节点文本必须清洗**：mermaid mindmap 对 `()` `:` `"` 等字符敏感，会直接破坏语法，需转义 + 截断
- **深度可控**：`--mindmap-depth {figure|panel|group|result}`，默认 `group`。
  一篇 10 图论文若展开到 result 层会有上百节点，糊成一团
- **状态标记**：`confidence: low` 或含 `inferred` 字段的节点加 `⚠` 前缀，
  让导图同时承担「哪里需要人工复核」的提示作用
- **纯函数**：入参只有 `figures.json`，不读 PDF、不调模型

---

## 6. 分阶段计划

每阶段结束都有可运行的东西，不做大爆炸式开发。

### P0 · 骨架（0.5 天）

- `scripts/paper_figure_notes.py` CLI 框架：`--pdf` `--out` `--profile` `--dpi` `--pages` `--mindmap-depth`
- `SKILL.md` + `README.md` 初版，挂 junction
- **验收**：`--dry-run` 能打印出解析计划

### P1 · L1 解析层（1 天）

- 排版自适应分派 + 两条切图路线（原型代码已跑通，移植即可）
- caption 跨页配对（三级回退）+ `(continued)` 跨页图合并 ← **本阶段真正的新工作量**
- 表格双通道（带状裁剪图 + 文本网格）
- 章节树（标题层级）、正文段落带 `§` 编号
- 三级降级兜底 + `bbox_confidence` 标记
- **验收**：两篇已验证样本 100% 正确切割；再随机取 2 篇新论文，准确率 ≥ 95%

### P2 · L2 装配层（0.5 天）

- 交叉引用扫描、Methods 关键词召回、caption 子图切分
- 产出 `context/fig_XX.md`（人可读，便于调试模型为何抽错）
- **验收**：随机抽 5 张图，其 context 包含了人工整理时会用到的全部原文

### P3 · L3 抽取层（1 天）· 质量核心

- `schemas/figure.schema.json` + 抽取提示词（含两条铁律、双 profile 规则、few-shot）
- **模式 A**：SKILL.md 指导 agent 逐图 Read PNG + context → 写 JSON
- **模式 B 接口预留**：抽取逻辑与调用方式解耦，`--api` 走 OpenAI 兼容端点
  （本机已有 `openai` SDK 与 `/v1` 端点，届时补 30 行即可）
- 结果按 schema 校验，失败字段回退 `not_reported` 并记 warning
- **验收**：挑一篇熟悉的论文逐图对照人工理解打分；`n`/剂量/统计三项零脑补是硬指标

### P4 · L4 渲染层（1 天）

- `figure-notes.md`：图 + 实验设计表 + 分组结果表 + 溯源标注
- **思维导图三个层级 + 三种格式**（§5）
- `all_groups.xlsx`：一行一组的长表（论文/图/panel/组名/角色/条件/n/指标/结果/来源）
- 跨文献汇总：合并多篇长表，支持「筛出所有用了 X 模型/X 剂量的实验」
- **验收**：3 篇论文的汇总表能回答一个真实的横向比较问题；导图在 Obsidian 里渲染正常

### P5 · 打磨（按需）

- 分阶段缓存 + 断点续跑（切图/context/抽取/渲染各自可跳过重跑）
- 与 `paper-search` skill 打通：搜索 → 下载 PDF → 直接进本流水线
  （已实测：走 `--sources europepmc` 返回的 `pdf_url` 直下可用；
  `download --platform pmc` 会被 PMC 的浏览器验证挡住，不要用）
- 补充材料（Supplementary）合并处理
- 扫描版 PDF 检测：无文本层时明确报错而非静默出垃圾

---

## 7. 已知风险

| 风险 | 影响 | 对策 |
|------|------|------|
| ~~生物医学复杂拼版大图~~ | ~~切图失败~~ | ✅ **已排除**：实测期刊排版比 LaTeX 更好切，见 feasibility.md §5 |
| 图题与图跨页分离 / `(continued)` | 切出废条、同图被拆成两条记录 | 三级回退配对 + 图号合并，P1 重点 |
| 表格 bbox（几何法已证伪） | 结果数字读不全 | 已改双通道方案，见 feasibility.md §3 |
| 模型脑补 n / 剂量 / 统计 | **直接毁掉文献整理的可信度** | 两条铁律 + `evidence.inferred` 溯源 + P3 人工对照验收 |
| 导图节点过多糊成一团 | 可读性归零 | `--mindmap-depth` 分层控制，默认只到 group 层 |
| 扫描版 PDF | 完全无法处理 | 检测文本层，缺失则明确报错，不静默降级 |
