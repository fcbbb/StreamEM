# 语义锚点实验结论（v1 基线）

## 1. 结论摘要

本轮语义锚点实验最终采用 **v1 Prompt** 作为基线。

v1 的主要结论如下：

1. v1 能够为大多数片段生成语义合理、可独立理解的 anchor。
2. 在当前 95 对评测样本上，v1 的 PR-AUC 为 `0.850644`，最佳 F1 为 `0.809524`。
3. 如果优先保证不产生错误连边，阈值约为 `0.703216` 时 Precision 为 `1.000000`，但 Recall 只有 `0.523810`，说明主要问题是漏连而不是误连。
4. 消费类是最明显的边界风险，尤其是早餐、午餐和咖啡之间的相似表达。
5. `session_0002_seg003` 中的 `A relaxed day with little physical activity` 主要是上游片段包含多个主题、且模型选择了后半段主题造成的提取错误。如果将该 anchor 修正为纯咖啡表达，整体评测会明显提升。
6. v2 虽然增加了“优先保留用户自身事实”的规则，但整体指标低于 v1，因此本实验采用 v1，不采用 v2。

## 2. 实验对象和数据

### 2.1 输入片段

- Pilot 样本数：60 条
- 输入文件：[`pilot_sample_60.jsonl`](artifacts/anchor_dataset_v1/pilot_v1/pilot_sample_60.jsonl)
- 每个片段单独调用一次模型
- 输入不包含其他片段的 anchor，也不包含 gold reference

### 2.2 v1 提取结果

- 结果文件：[`new_outputv1.jsonl`](artifacts/anchor_dataset_v1/output/new_outputv1.jsonl)
- 模型：`gpt-5.6-luna`
- 成功结果：60/60
- 非空 `selected_anchor`：59/60
- `null` anchor：1 条
- Prompt：[`anchor_extraction_prompt_v1.md`](artifacts/anchor_dataset_v1/pilot_v1/anchor_extraction_prompt_v1.md)

### 2.3 评测数据

- Pair 数量：95
- gold 正例：21
- gold 负例：74
- 排除 `relation=uncertain` 后的 definite pair：92
- Pair candidates：[`pilot_pair_candidates.jsonl`](artifacts/anchor_dataset_v1/pilot_v1/pilot_pair_candidates.jsonl)
- Pair gold：[`pilot_pair_gold_labels.jsonl`](artifacts/anchor_dataset_v1/pilot_v1/pilot_pair_gold_labels.jsonl)
- Segment gold：[`segment_gold_labels.jsonl`](artifacts/anchor_dataset_v1/segment_gold_labels.jsonl)

## 3. v1 Prompt 的定义和行为

v1 将 semantic anchor 定义为能够代表片段主要话题的稳定主题表达，而不是：

- 完整摘要
- 关键词列表
- 偶然出现的实体
- 完整事件描述

Prompt 要求 anchor 满足：

1. Representativeness：代表片段的主要内容
2. Distinctiveness：能够区分相近但不同的主题
3. Granularity：粒度适当，既不过宽也不过窄
4. Standalone interpretability：脱离对话后仍能理解

模型先生成三类候选：

- `coarse_candidate`：较宽的主题
- `selected_anchor`：最终选择的 anchor
- `fine_candidate`：更细的表达

v1 中还有一句重要但偏软的规则：

```text
The final anchor should prefer the form "specific object + specific aspect"
when appropriate.
```

这条规则对单个片段的表达有帮助，但没有强制要求：

- 同一项目在不同片段中必须保留相同的核心名称
- 同一主题的 anchor 必须使用 canonical topic
- 多主题片段应该如何选择主要主题
- 用户事实和聊天状态之间的优先级

因此，v1 生成的 anchor 通常“单条语义合理”，但跨片段对齐并不总是稳定。

## 4. v1 评测结果

完整结果见：

- [`output/summary.md`](artifacts/anchor_dataset_v1/output/summary.md)
- [`output/v1/pair_scores.md`](artifacts/anchor_dataset_v1/output/v1/pair_scores.md)
- [`output/v1/thresholds_all.md`](artifacts/anchor_dataset_v1/output/v1/thresholds_all.md)
- [`output/v1/thresholds_definite.md`](artifacts/anchor_dataset_v1/output/v1/thresholds_definite.md)

### 4.1 总体指标

| 指标 | v1 结果 |
| --- | ---: |
| Pair 数量 | 95 |
| 正例 | 21 |
| 负例 | 74 |
| PR-AUC / Average Precision | 0.850644 |
| PR-AUC / Trapezoid | 0.848231 |
| 最佳 F1 阈值 | 0.543523 |
| 最佳 F1 | 0.809524 |
| 最佳 F1 Precision | 0.809524 |
| 最佳 F1 Recall | 0.809524 |

### 4.2 高精度工作点

评测目标设置为 Precision `>= 0.95`。v1 选择的高精度工作点为：

| 指标 | v1 结果 |
| --- | ---: |
| 阈值 | 0.703216 |
| TP | 11 |
| FP | 0 |
| TN | 74 |
| FN | 10 |
| Precision | 1.000000 |
| Recall | 0.523810 |
| F1 | 0.687500 |
| False edge rate | 0.000000 |

这说明 v1 在保守阈值下不会产生错误连边，但会漏掉约一半 gold 正例。因此不能把 `0.703216` 理解为“总体最优阈值”，它是偏向高精度的工作点。

如果更看重 Precision 和 Recall 的平衡，最佳 F1 阈值约为 `0.543523`，此时 Precision 和 Recall 都约为 `0.81`。

## 5. 主要错误模式

### 5.1 消费类是最明显的边界风险

消费相关负例之间的相似度偏高，例如：

```text
Breakfast spending <> Morning coffee spending       0.702107
Morning breakfast purchase <> Morning coffee spending 0.597951
Breakfast spending <> Lunch spending                0.594808
```

因此当阈值降低到 `0.70` 左右时，早餐和咖啡之间可能形成错误连边。

同时，真正的咖啡正例相似度跨度很大：

```text
0.154549, 0.195000, 0.304487, 0.683675, 0.709258, 0.718231
```

这表明消费类的主要问题不仅是“不同消费主题太相似”，还包括“同一消费主题的 anchor 表达不一致”。

### 5.2 `session_0002_seg003` 的咖啡主题被忽略

原始片段同时包含：

```text
I just spent $3.66 on a coffee...
How many steps did you get today?
Not many, actually. It's been a pretty relaxed day.
```

v1 输出为：

```text
A relaxed day with little physical activity
```

模型的 reason 明确写道：

```text
The coffee transaction is incidental.
```

问题在于模型把“金额是次要细节”错误扩展成了“咖啡交易整体是次要主题”。实际上，金额可以是次要信息，但“coffee purchase”本身是用户明确表达的事实。

该片段在数据元信息中同时带有 `food_coffee` 和 `steps` 两个标签，但这些标签没有传给模型。它还被 pilot 归入了 `food_coffee` 的 repeated-topic positive，因此这是上游片段多主题和目标主题选择共同造成的问题。

### 5.3 项目类主要是 canonical topic 没有稳定保留

#### Pair 00079：相似度 0.567660

```text
Secure federated learning framework for privacy-compliant regional healthcare data analysis
Removal of deliverable and stakeholder entries from the Secure Federated Learning Framework project proposal
```

两个 anchor 都有语义依据，但一个突出项目本身，另一个突出删除动作和操作对象。稳定项目身份没有位于两个表达的共同核心位置。

#### Pair 00084：相似度 0.624380

```text
Deep learning for regional energy demand forecasting under climate volatility
Updates to the regional energy demand forecasting project proposal
```

第二个 anchor 省略了 `deep learning` 和 `climate volatility`，导致“项目主题”和“项目更新动作”之间的表达不一致。

这类问题不是模型完全理解错误，而是缺少“先保留稳定项目名，再补充具体动作/方面”的跨片段规范。

### 5.4 步数类是同义表达和粒度不一致

```text
Daily walking activity <> Daily step count       0.581972
```

两个 anchor 实际表达接近，但一个强调行为，一个强调指标。v1 没有要求使用稳定的 canonical topic，例如：

```text
Daily step-count and walking activity
```

### 5.5 机器学习类部分是 gold 粒度问题

```text
Types of machine learning and their practical applications
How machine learning learns from data
```

相似度为 `0.543523`。两者属于 machine learning，但一个讨论类型和应用，一个讨论学习机制和数据。模型输出本身并不明显错误；更像是 gold 将“machine learning fundamentals”这一大主题下的不同子方面标成了同一 specific topic。

### 5.6 XAI 与 AI 艺术可能是标注边界过宽

```text
Explainable AI transparency–performance trade-offs in complex AI systems
Criteria for recognizing AI-generated works as legitimate art
```

相似度仅为 `0.372327`，但 gold 标记为同一 specific topic。对应错误记录还将右侧记忆粒度标记为 `too_coarse`。这条更像 gold 主题边界问题，不应简单归因于模型提取失败。

## 6. 只修正咖啡 anchor 的反事实结果

以下结果没有修改原始 v1 文件，只在内存中将：

```text
A relaxed day with little physical activity
```

替换为咖啡表达，然后使用同一个 `all-MiniLM-L6-v2` 重新计算。

### 6.1 替换为 `Coffee purchase as a daily expense`

| 指标 | 原 v1 | 反事实结果 |
| --- | ---: | ---: |
| PR-AUC / Average Precision | 0.850644 | 0.943806 |
| 最佳 F1 | 0.809524 | 0.909091 |
| 高精度阈值 | 0.703216 | 0.703216 |
| 高精度 Precision | 1.000000 | 1.000000 |
| 高精度 Recall | 0.523810 | 0.619048 |
| 高精度 F1 | 0.687500 | 0.764706 |

受影响的正例：

```text
pair_00089：0.195 → 0.718
pair_00104：0.155 → 1.000
pair_00105：0.304 → 0.684
```

与步行主题的无关 pair 也从 `0.566` 降至约 `0.224`。

### 6.2 替换为 `Coffee spending`

这个更短、更偏 canonical topic 的表达结果为：

| 指标 | 结果 |
| --- | ---: |
| PR-AUC / Average Precision | 0.940900 |
| 最佳 F1 | 0.888889 |
| 高精度 Precision | 1.000000 |
| 高精度 Recall | 0.666667 |
| 高精度 F1 | 0.800000 |

这说明只修正这一条上游 anchor，v1 整体表现就会明显提升。该反事实结果不是当前已落盘的正式评测文件，正式 v1 结果仍以 `new_outputv1.jsonl` 和 `output/v1/` 为准。

## 7. v1 与 v2 对比

v2 在 v1 基础上增加了以下规则：

```text
If the user explicitly states a fact about their own actions, experiences,
preferences, plans, or circumstances, treat that user-specific fact as
substantive content and prefer it over a vague conversational state when
choosing the main topic.
```

v2 的结果文件为 [`new_outputv2.jsonl`](artifacts/anchor_dataset_v1/output/new_outputv2.jsonl)，评测结果为 [`output/v2/summary.md`](artifacts/anchor_dataset_v1/output/v2/summary.md)。

| 指标 | v1 | v2 |
| --- | ---: | ---: |
| 成功输出 | 60/60 | 60/60 |
| 非空 anchor | 59 | 59 |
| Anchor 文本发生变化 | — | 47/60 |
| PR-AUC / Average Precision | 0.850644 | 0.748823 |
| 最佳 F1 | 0.809524 | 0.702703 |
| 高精度 Recall | 0.523810 | 0.285714 |

在相同阈值 `0.70` 下：

```text
v1：TP=11, FP=1, FN=10
v2：TP=7,  FP=2, FN=14
```

v2 变差的主要原因：

1. 模型大量加入 `User's`、`Personal experience of` 等低信息量前缀。
2. 一些原本稳定的主题被扩展成“用户事实 + 情绪/附带方面”。
3. 咖啡多主题片段仍然被判断为 relaxed day，核心上游问题没有解决。
4. 新规则没有要求多个相关片段保留相同的 canonical topic。

因此，本实验最终保留 v1 作为基线，不采用 v2 作为正式结果。

## 8. 最终建议

### 8.1 当前正式基线

正式基线为：

- Prompt：v1
- 模型：`gpt-5.6-luna`
- Embedding：`all-MiniLM-L6-v2`
- 输出：`new_outputv1.jsonl`
- 评测目录：`output/v1/`

### 8.2 连边阈值

如果论文或系统更重视“不产生错误连边”，可报告高精度工作点：

```text
threshold = 0.703216
precision = 1.000000
recall = 0.523810
```

如果更重视整体平衡，可报告最佳 F1 工作点：

```text
threshold = 0.543523
precision = 0.809524
recall = 0.809524
f1 = 0.809524
```

阈值应根据应用目标选择，不应只报告一个脱离目标的数字。

### 8.3 下一步改进优先级

1. 优先修复上游多主题片段的归属和切分，特别是 `session_0002_seg003`。
2. 对项目类片段保留稳定项目名称，再追加更新、删除、预算等具体方面。
3. 对消费类片段统一使用 `coffee spending`、`breakfast spending`、`lunch spending` 等稳定主题表达。
4. 后续 Prompt 版本应规定“canonical topic + aspect”，但不必强制使用某种特殊标点。
5. 对机器学习、XAI/AI 艺术等边界样本重新检查 gold 粒度，避免把同一大类下的不同子主题全部视为 same specific topic。

## 9. 复现实验命令

以下命令在 `F:\StreamEM` 的 PowerShell 中执行。

### 9.1 使用 v1 Prompt 提取

```powershell
uv run streamem-anchor-extract --method candidate_selection_v1 --prompt F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\pilot_v1\anchor_extraction_prompt_v1.md --output F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\new_outputv1.jsonl --model gpt-5.6-luna --env-file F:\StreamEM\.env --no-proxy --resume
```

### 9.2 评测 v1

```powershell
uv run streamem-anchor-eval --method "v1=analyse3/anchor/artifacts/anchor_dataset_v1/output/new_outputv1.jsonl" --pairs "analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/pilot_pair_candidates.jsonl" --gold "analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/pilot_pair_gold_labels.jsonl" --segment-gold "analyse3/anchor/artifacts/anchor_dataset_v1/segment_gold_labels.jsonl" --embedding-model all-MiniLM-L6-v2 --precision-target 0.95 --out-dir "analyse3/anchor/artifacts/anchor_dataset_v1/output"
```

主要查看：

```text
F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\summary.md
F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\v1\pair_scores.md
F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\v1\thresholds_all.md
F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\v1\all_false_negative.jsonl
```

## 10. 最终判断

v1 已经足以作为当前实验的正式基线：它的 anchor 大多语义合理，错误主要集中在上游多主题片段、消费类主题边界、跨片段 canonical topic 不一致，以及少量 gold 粒度问题。

因此当前最合理的表述不是“v1 模型整体提取失败”，而是：

> v1 的单片段语义提取质量总体良好，但跨片段连边效果受上游主题归属和 anchor 规范化影响；消费类是最明显的风险区域。修正关键上游 anchor 后，v1 的排序和连边表现可以显著改善。
