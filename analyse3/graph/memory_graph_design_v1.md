# Memory Graph 初步设计 v1

## 1. 设计目标

系统需要同时满足四个目标：

1. 新证据能够利用已有 memory，而不是每次都从零发现主题；
2. 已经经过 LLM 确认的 memory 作为一个完整单元，不被普通社区检测拆开；
3. 新证据仍然可以发现已有 memory 中不存在的新主题；
4. memory 与 memory 的关系不在底层证据社区中被错误合并，而由上层单独处理。

核心判断是：

> 已确认的 memory 是稳定的压缩单元；未确认的 segment 是可以被重新聚类的候选证据。

## 2. 术语和相关方法调查

文献中的 *seeded community detection* 通常指 seed-set expansion：给定少量已知属于目标社区的种子节点，再通过局部随机游走、PageRank 或局部谱方法扩展社区。LEMON 属于这一类，重点是从种子局部发现社区，且可以支持重叠社区，而不是对全图进行带固定标签的社区分区。

参考：[LEMON: Overlapping Community Detection via Local Spectral Clustering](https://arxiv.org/abs/1509.07996)

本项目的需求更接近：

> 带部分已知社区标签的动态半监督社区检测，并允许新社区产生。

相关研究包括：使用已知标签和 pairwise constraints 指导模块度/自旋玻璃社区检测，以及根据已有社区标签预测新节点社区归属的方法。

参考：[A Spin-Glass Model for Semi-Supervised Community Detection](https://ojs.aaai.org/index.php/AAAI/article/view/8320)、[Semi-supervised Community Detection via Structural Similarity Metrics](https://arxiv.org/abs/2306.01089)

因此本文不把方案称为一个已经存在的“标准 seeded Leiden”算法，而称为：

> 带固定 memory 标签和新社区发现能力的动态半监督社区检测。

## 3. 两层图结构

### 3.1 底层活动图：证据归属和新社区发现

底层活动图中包含两种节点：

```text
已确认的 memory node
尚未压缩的新 segment node
```

允许的边：

```text
新 segment ─ 新 segment
新 segment ─ memory
```

不允许的边：

```text
memory ─ memory
```

底层社区的含义是：

- 社区中包含一个已有 memory：新段候选更新该 memory；
- 社区中没有已有 memory：这是候选新主题，之后可以提取新 memory；
- 一个社区不能包含两个已有 memory。

### 3.2 上层 memory graph：memory 之间的关系

上层图只包含已经确认的 memory node。它负责表示：

- 相关主题；
- 近似重复；
- 互补关系；
- 时间演化；
- 矛盾关系。

上层的 memory 社区不等于底层证据社区，也不自动要求合并 memory。两个 memory 可以相关，但仍保持为两条独立记忆。

## 4. 底层算法设计

### 4.1 节点状态

```text
new：新到达、尚未确认的 segment
committed memory：已经经过 LLM 确认的 memory
boundary：同时接近多个 memory、暂时无法确定归属的 segment
```

### 4.2 边构建

初版沿用当前实验中表现较好的规则：

```text
kNN k=10 + similarity threshold 0.5
```

它作为图稀疏化和候选关系生成规则，不直接承担最终的记忆归属判断。

初版允许：

```text
new-new 边：新段之间的语义相似关系
new-memory 边：新段与 memory 摘要/稳定主题表示之间的候选关系
```

`new-memory` 的阈值不应直接假定与 `anchor-anchor` 的 0.5 完全等价，后续需要单独校准；在初版中可以先复用 0.5 作为候选边阈值，并把最终归属交给社区约束和 LLM 校验。

### 4.3 带固定 memory 的增量社区检测

每次新增数据时使用上一轮社区结果作为初始状态：

```text
memory_i 的社区标签固定
new segment 的社区标签可变
允许 new segment 创建无 memory 的新社区
一个社区最多包含一个已有 memory
```

这不是普通 Leiden。普通 Leiden 即使没有 memory-memory 边，也可能通过桥接新段把两个 memory 放进同一社区。因此实现必须支持固定节点和 cannot-link 约束；仅设置 `initial_membership` 不等于固定标签。

如果当前 Leiden 实现不能直接表达这些约束，则采用以下等价的工程实现：

1. 在包含 new-new 和 new-memory 边的图上运行增量社区优化；
2. 固定 memory 的标签，不允许 memory 被移动；
3. 禁止产生包含多个 memory 的社区；
4. 对违反约束的社区按 memory 种子拆分，或将相关新段标为 boundary；
5. 没有 memory 的部分保留为新社区候选。

## 5. 新证据的处理流程

```text
新 segment 到达
    ↓
加入底层活动图
    ↓
与已有 memory 和其他新段建立候选边
    ↓
增量社区检测
    ↓
社区中有一个已有 memory？
    ├─ 是：作为该 memory 的候选补充证据
    └─ 否：作为候选新主题社区
    ↓
提取记忆前进行 LLM 校验/纯化
    ↓
更新已有 memory 或创建新 memory
```

LLM 处理的不是每个普通新段，而是以下情况：

- 社区内部存在明显多个主题；
- 一个社区同时接近多个 memory；
- 新社区需要提取长期 memory；
- 新证据可能改变已有 memory 的主题范围。

普通新增证据如果属于已有 memory，只更新该 memory 的内容、统计信息和来源列表。memory 的身份和上层 memory-memory 关系默认不改变。

## 6. Memory 的稳定性和版本

memory 不应被视为每次聚类都会变化的普通节点。建议分开保存：

```text
稳定主题表示：用于建边和社区归属，默认保持稳定
记忆内容：事实、时间、数值、来源，可以增量更新
```

例如新证据只是重复确认已有记忆时：

```text
memory_id 不变
topic embedding 不变
summary 内容补充或统计更新
memory-memory 边不变
```

只有出现主题范围实质变化、矛盾事实或需要拆分时，才创建新版本或新 memory。旧版本保留用于追溯。

## 7. 压缩策略

当一个底层社区经过 LLM 确认后，才将它视为可压缩单元：

```text
segment A、B、C
    ↓
memory M
```

活动图中可以用 M 作为这些证据的代表节点；原始 segment 保留在归档层，并记录：

```text
memory_id → source_segment_ids
```

这样活动图规模可以压缩，但仍能在需要精确事实、引用或重新提取时回溯原始证据。

### 7.1 Memory 作为超节点

压缩后，memory 不应只是一个拥有单条普通边的摘要节点，而应作为原社区的超节点。超节点用一个节点代表原社区，但需要保留原社区对外连接的聚合信息。

原始图可能存在：

```text
x ─ A，similarity=0.72
x ─ B，similarity=0.68
x ─ C，similarity=0.63
```

压缩后图中可以变为：

```text
x ─ memory_M
```

但这条逻辑边需要记录：

```text
support_count
max_similarity
mean_similarity 或 mean_top_r_similarity
supporting_segment_ids（可选）
```

不能只用 memory summary 与 x 的一次 embedding 相似度，否则会丢失 x 获得多个社区成员支持这一结构信息。

边权有两种主要语义：

```text
结构保持型：w(x, M) = 所有原始外部边权之和
语义匹配型：w(x, M) = Top-r 支持边的平均值，并保留 support_count
```

求和更接近原图收缩后的结构，但会使大社区天然拥有更大的吸引力；归一化聚合可以降低社区规模偏置。初版建议实验比较两类聚合方式，不应默认“边权越大越好”。

超节点设计的目标是：

- 用一个节点实现活动图压缩；
- 保留原社区对新证据的多节点支持；
- 避免大社区仅因规模大而吸收过多新段；
- 允许压缩后的图继续进行社区检测。

## 8. 评估方案

### 8.1 底层社区质量

- same-specific pairwise Precision、Recall、F1；
- overmerge rate；
- oversplit rate；
- 社区内部相似度和低分位数；
- singleton 和孤立节点比例；
- 社区稳定性和节点迁移率。

### 8.2 新段归属质量

- 新段正确归属已有 memory 的准确率；
- 新主题被错误吸收到旧 memory 的比例；
- 新社区最终成功提取 memory 的比例；
- boundary 节点比例；
- 归属后 LLM 纠正比例。

### 8.3 记忆质量

- 记忆事实覆盖率；
- 来源证据可追溯率；
- 摘要事实一致性；
- 重复信息压缩率；
- broad 问题的 evidence recall；
- 记忆更新后的答案质量。

## 9. 需要避免的误区

1. 不把普通 Leiden 的 `initial_membership` 当成固定 memory 标签；
2. 不因为两个 memory 被同一个新段连接，就自动合并两个 memory；
3. 不用单个 anchor 相似度直接决定最终记忆归属；
4. 不把 Community F1 直接当成记忆摘要质量；
5. 不在 memory 尚未确认前就从局部社区直接生成长期记忆；
6. 不因普通新增证据而重新提取所有历史 memory。

## 10. 初步结论

当前建议采用以下设计作为下一步实验方向：

```text
底层：memory + new segment 的带固定标签约束的增量社区检测
边：new-new、new-memory；禁止 memory-memory
已有 memory：固定身份，不被 Leiden 拆分或合并
新段：可加入已有 memory，也可形成新社区
LLM：只处理混合社区、边界节点和新 memory 提取
上层：单独维护 memory-memory 关系图
压缩：LLM 确认后才将证据社区压缩为 memory node
```

它比“普通增量 Leiden”更符合 memory 已经经过 LLM 确认、应作为稳定整体的定义；也比“先局部聚类、再独立匹配 memory”保留更多全局约束。

当前仍需通过实现和实验验证：

- 固定 memory 的增量 Leiden 约束如何落地；
- `new-memory` 边阈值是否需要独立于 `anchor-anchor`；
- boundary 节点应拆分、延迟处理还是允许多归属；
- 压缩后活动图和上层 memory graph 的更新频率。
