# 锚点图构建与社区检测实验方案 v1

状态：方案讨论稿

## 1. 目标

使用 277 个已筛选片段的 anchor，比较不同建边规则对最终社区结果的影响，确定一套后续流式系统使用的默认方案。

主问题不是“哪些节点有直接边”，而是：

> 两个节点是否最终被社区检测算法分到同一社区，并且这个社区是否对应同一具体主题。

## 2. 输入与输出

### 输入

每个片段需要有：

```text
segment_id
session_id
anchor_text
embedding
```

当前实验暂按一个片段对应一个节点处理。anchor 文本需要固定使用一个字段，例如 `selected_anchor` 或 `fine_candidate`，不能混用。

评估标签使用：

- `pair_candidates.jsonl`：根据 `pair_id` 找到两个片段；
- `pair_gold_labels.jsonl`：提供 `same_specific_topic` 和 `relation`。

标签只用于评估，不参与建图。`candidate_types`、`candidate_reasons` 和 `heuristic_tags` 不作为 gold label。

### 输出

每个节点得到一个社区编号：

```text
segment_id -> community_id
```

同时保存社区数量、社区大小和社区代表性 anchor，便于人工检查。

## 3. 实验流程

```text
277 个 anchor
    ↓
生成 embedding 和相似度矩阵
    ↓
按不同规则建加权图
    ↓
Leiden 社区检测
    ↓
得到 community_id
    ↓
与 pair 标注比较
```

277 个节点的全部无序 pair 为 38,226 对。建图时原则上使用所有节点的相似度；当前 1,576 个候选 pair 主要用于评估，不应把候选生成规则当成图规则。

## 4. 建边规则对照

### M0：固定阈值

```text
similarity(i, j) >= 0.50 连接
```

`0.50` 只作为历史基线，同时扫描一组阈值观察敏感性。

### M1：普通 kNN

每个节点连接相似度最高的前 `k` 个节点，初始测试：

```text
k = 5, 10, 20
```

### M2：mutual kNN

只有双方都把对方选入 top-k 时才连接。预期边更少，但边精度可能更高。

### M3：kNN + 相似度阈值

在 kNN 选边后，再要求：

```text
similarity(i, j) >= 0.50
```

### M4：mutual kNN + 相似度阈值

同时要求双方互为 top-k，且相似度不低于 `0.50`。这是当前重点关注的组合方案。

### 边权

默认保留连续边权：

```text
w(i, j) = similarity(i, j)
```

二值边权只作为消融对照。

## 5. 社区检测算法

### 主算法：Leiden

Leiden 适合作为默认算法，因为它在 Louvain 的基础上增加了社区细化，通常能减少社区内部不连通的问题。需要扫描 resolution，避免社区过度合并或过度切分。

### 对照算法：Louvain

Louvain 只作为传统基线。在同一张图、相同边权和相同 resolution 下运行一次，用于验证 Leiden 在当前数据上的实际收益。

最终是否保留 Louvain，不依据算法名称判断，而依据社区级指标和稳定性判断。

### 辅助基线：连通分量

可选。用于观察纯阈值连通的结果，但不作为最终社区算法，因为容易产生链式合并。

## 6. 主要评估指标

对每个已标注 pair，定义：

```text
gold_same = same_specific_topic
pred_same = community_id(left) == community_id(right)
```

排除 `uncertain` 后计算：

```text
Precision = TP / (TP + FP)
Recall    = TP / (TP + FN)
F1        = 2 × Precision × Recall / (Precision + Recall)
```

这些是社区级 pairwise 指标，优先级高于“直接边是否存在”的边级指标。

解释：

- Precision 低：不同主题被合并，过度合并；
- Recall 低：同一主题被拆开，过度切分；
- F1：综合指标。

当前标签中有 224 个正例、1,281 个确定负例和 71 个 uncertain pair。由于只标注了 1,576/38,226 个 pair，指标必须表述为“当前候选 pair 上的结果”。

## 7. 辅助观察项

每种“建边规则 + 社区算法”至少记录：

- 社区数量；
- 最大社区占比；
- 单节点社区比例；
- 平均度和边密度；
- 孤立节点比例；
- 社区结果对阈值、k 和 resolution 的敏感性；
- Leiden 与 Louvain 的社区划分稳定性。

同时抽查每个较大社区的 anchor，检查是否真的围绕同一具体主题，特别关注共享实体但主题不同的情况。

## 8. 方案选择

优先选择：

1. 社区级 Precision 较高，错误合并较少；
2. Recall 和 F1 保持可接受水平；
3. 社区规模没有明显异常；
4. 参数轻微变化时结果稳定。

预期默认组合为：

```text
mutual kNN + 连续相似度边权 + Leiden
```

但最终以实验结果为准。

## 9. 流式阶段

先在固定的 277 个节点上确定：

- anchor 字段；
- embedding 和相似度定义；
- 建边规则；
- Leiden resolution。

之后再进入流式实现：

```text
新片段
→ 提取 anchor 和 embedding
→ 检索历史节点中的 top-k 候选
→ 按冻结的规则加边
→ 局部更新社区
→ 定期全量运行 Leiden 校正
```

流式阶段重点观察新节点处理延迟、社区稳定性、社区合并/拆分次数，以及定期全量 Leiden 前后的差异。

当前 277 个 pair 标注主要用于确定静态方案，不能充分评估长期流式漂移；后续需要补充按时间到达的验证数据。
