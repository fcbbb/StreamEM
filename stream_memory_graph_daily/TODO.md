# Stream Memory Graph TODO

更新时间：2026-09-12

## 待办一：社区纯化与 Boundary 处理

- [ ] 分析现有 `boundary`：区分真实主题交叉、anchor 过粗、memory 过宽、embedding 误连和证据不足。
- [ ] 接入社区纯化：放在固定 memory 拆分和 Boundary 标记之后、memory extraction/fusion 之前。
- [ ] 纯化必须保证每个 `segment_id` 恰好出现一次，不生成或删除 segment，并记录原始输入、输出、分组映射和错误。
- [ ] 暂不自动归属、删除或强制融合 Boundary；纯化失败时保守回退，不能把混合社区直接写入 memory。
- [ ] 对比无纯化、纯化但不处理 Boundary、纯化并进行明确 Boundary 后处理三种方案，评估社区纯度、Boundary、memory 数量和最终检索/回答效果。

纯化接入顺序：

```text
活动图
→ 固定 memory 拆分 / Boundary 标记
→ 社区纯化
→ memory extraction / fusion
→ 归档 segment
```

## 待办二：关键词/实体增强的高召回建图

- [ ] 为每个 segment 和 memory 维护关键词集合、实体集合；memory 同时保留聚合集合及历史成员集合。
- [ ] 建图时合并语义、关键词和实体候选边；实体或稀有关键词可形成弱边，统一计算边权后交给 Leiden。
- [ ] 检查阈值和 top-k 逻辑，避免词法/实体候选在高召回建图阶段被过滤掉。
- [ ] 对比仅语义建图、加入关键词、加入实体、两者都加入四种方案，重点评估同主题召回率和跨主题误连率。

## 待办三：多层长期记忆的第一版方案

### 设计目标与基本约束

第一版采用以下约束：

1. 层级表示主题范围和粗粒度：越高层可以覆盖更多方面的共同主题，但高层不等同于“更旧”。
2. 一个主题在活动系统中只有一个当前表示，即一个 `topic lineage` 只有一个 active owner。
3. 高层 memory 晋升后，全局替换低层 memory：低层节点从所有活动图和唤醒索引中移除；原始证据和来源关系是否保留由独立的 retention/forgetting 策略决定。
4. 原始 segment 仍然先进入 L0 的局部构建；高层快速路径使用的是由 L0 社区形成的临时 L1 表示，不把原始 segment 直接放进高层社区图。
5. 社区检测图不混合任意层级。第 `k` 层只处理第 `k` 层当前节点和第 `k+1` 层活动摘要；跨层候选匹配使用独立的主题归属路由器，不产生 Leiden 图边。
6. 正常压缩逐层进行；快速路径允许临时 L1 直接融合到已经存在的 L2/L3 owner，但不是让原始 L0 直接越级写入高层。

### 层级和活动图

建议的逻辑层级：

```text
L0：原始事件 segment，只是输入证据和当前局部社区节点
L1：近期/局部主题记忆
L2：跨 L1 的较粗主题
L3：更长期、更宽的主题结构
```

为每个层级维护一个边界图：

```text
G0 = L0 当前 segment + 尚未晋升的 L1 memory
G1 = L1 当前 memory + 尚未晋升的 L2 memory
G2 = L2 当前 memory + 尚未晋升的 L3 memory
```

每个图只建立：

- 当前层节点之间的边；
- 当前层节点与上层活动 memory 之间的边；
- 不建立跨越多个层级的社区边；
- 不把 L0、L1、L2、L3 混在一张 Leiden 图中。

高层替换低层时，低层 memory 从所有活动图中移除。例如 `L1-A → L2-B` 成功后，`L1-A` 不再出现在 `G0`、`G1` 或全局主题路由索引中；`L2-B` 成为该主题的唯一活动 owner。

### 新数据的完整处理流程

#### 阶段 A：L0 局部构建

1. 新 segment 写入 `SegmentRecord`，进入 L0 pending 集合。
2. 将它加入 `G0`，只和 L0 segment 以及尚未晋升的 L1 memory 建边。
3. 对受影响的 L0 连通区域执行 Leiden、社区纯化和边界处理。
4. 每个纯化后的 L0 社区形成一个临时 L1 表示：

```text
provisional_L1 = topic + summary + structured items + source segment ids
```

临时 L1 还没有决定最终是否作为 L1 保存。

#### 阶段 B：全层主题归属路由

对每个 `provisional_L1`，查询所有层级的活动 memory，但这一步不是社区检测，也不产生图边。

每个活动 memory 维护轻量的 `wake_signature`：

- topic 向量；
- 关键词和实体；
- 少量直接成员 anchor；
- level；
- `topic_lineage_id`；
- 当前 owner 的 memory ID。

路由器输出的不是一串未经处理的相似度，而是以下四种结果之一：

```text
OWNER(memory_id, level, confidence)
NEW_TOPIC
AMBIGUOUS
REJECTED
```

路由分数必须按层级校准，不能直接比较不同层的原始 cosine。需要考虑：

- 当前层级的相似度基线；
- 候选与第二名的 margin；
- 主题/实体重合；
- 冲突信息；
- 该候选是否属于同一 `topic_lineage`。

如果一个主题已经从 L1 晋升到 L3，旧 L1/L2 owner 已从索引移除，因此同一主题不会同时返回多个层级的活动 owner。不同层级仍可能出现语义相似但不属于同一主题的候选；若无法区分，结果必须是 `AMBIGUOUS`，不能强行融合。

#### 阶段 C：快速晋升或普通路径

对每个临时 L1 分两条路径处理。

**快速路径：**

```text
provisional_L1 → 唯一活动 owner（可能位于 L1/L2/L3）
```

执行目标层级的 fusion/purification：

- 原始 segment 仍作为 evidence 保存；
- 临时 L1 作为统一粒度的更新表示；
- 直接更新目标 owner；
- 不把临时 L1 作为新的长期活动节点保存；
- 成功后归档对应 L0 segment，并更新 owner 的直接成员表示和 wake signature。

快速路径只在 owner 唯一、置信度足够、没有明显冲突时使用。快速融合失败时，必须保留临时 L1，回退到普通路径，不能丢失证据。

**普通路径：**

```text
没有唯一高层 owner
  → provisional_L1 正式保存为 L1
  → 加入 G1
  → L1 社区检测 + purification
  → 与 L2 融合，或形成新的 L2
```

普通路径中的每一次融合仍然只向上走一层。产生的 L2 可以在同一个 checkpoint 内继续进入 G2，但表示必须先完成 L1→L2 的转换。

### 候选冲突和局部社区边界

如果一批 L0 在局部语义上相似，但它们的高层 owner 不同：

```text
L0-A、L0-B → L2-X
L0-C       → L2-Y
```

不能先把三者无条件合成一个 L1。应按以下顺序处理：

1. 先用 L0 局部图发现候选社区；
2. 汇总社区内所有 L0 对 owner 的支持；
3. 如果 owner 冲突明显，调用 purification 拆分社区；
4. 如果仍无法决定，标记为 `AMBIGUOUS/Boundary`，暂不快速融合；
5. 只有一个临时 L1 对应一个明确 owner 时，才允许快速晋升。

因此，单个 L0 对多个高层 memory 的原始相似度只是证据，不是最终归属；最终归属单位是临时 L1 社区。

### 正常晋升规则

快速路径处理的是“新局部记忆回到已有主题”。独立的正常晋升处理的是“多个同层 memory 形成更粗主题”：

```text
多个 L1 memory
  → G1 社区检测
  → purification
  → 创建/更新 L2
  → 全局移除被替换的 L1
```

然后对 L2→L3 重复同样过程。

初版不引入抽象的 stability 分数。是否允许正常晋升先使用可观测条件：

- memory 是否达到该层的最小证据量或时间跨度；
- 是否形成跨多个 checkpoint/event_date 的同层社区；
- 是否存在 Boundary 或未解决冲突；
- fusion 是否能产生明确的共同主题。

这些条件应作为层级策略配置，后续再根据评测结果调整，不直接用单一“稳定性分数”决定。

### 层级粗细与建图参数

越高层可以允许更宽的主题连接，但不能只降低相似度阈值。每层至少独立配置：

- current-current edge threshold；
- current-upper-memory edge threshold；
- Leiden resolution；
- kNN 或候选边上限。

高层通常使用更宽松的边阈值和更低的 Leiden resolution，以允许更粗主题，但必须配合 purification，防止低阈值造成链式误连和巨大社区。

阈值应按层级相似度分布校准，而不是直接复用 L0 的绝对阈值。

### 来源和表示

来源关系需要区分两种用途：

```text
直接成员表示：用于当前层与上层的边计算
完整来源关系：用于审计、重建、版本追踪和后续遗忘
```

例如：

```text
L1 的直接成员表示：L0 anchor
L2 的直接成员表示：L1 memory representation
L3 的直接成员表示：L2 memory representation
```

高层替换低层后，低层不再作为活动节点，但高层保留必要的直接成员表示和 wake signature；不需要在活动图中递归展开完整来源树。

### 压缩与遗忘

第一版明确区分：

```text
全局替换：从所有活动图和主题路由索引移除低层节点
持久化保留：SegmentRecord、来源和审计仍可暂时保留
真正遗忘：后续再决定是否删除完整原文，只保留 anchor/来源摘要
```

当前代码的 `_archive_segments()` 已经实现了活动图层面的移除，但状态中的 segment、source 和 audit 仍会增长。后续需要单独实现 retention/GC，不能把“从活动图移除”误认为“持久化数据已经遗忘”。

### 建议的实现顺序

- [ ] 将 `ActiveGraph` 泛化为按层维护的多个边界图，或实现 `LayeredActiveGraph`。
- [ ] 给 `MemoryRecord` 增加 `level`、`topic_lineage_id`、直接成员表示、owner 状态和晋升版本信息。
- [ ] 将当前 `source_anchors` 的边计算逻辑推广为“直接下层表示”，避免高层每次递归扫描所有原始 segment。
- [ ] 实现全局 `TopicOwnerRouter`：索引所有活动 memory，但不向社区图添加跨层边。
- [ ] 实现 `OWNER / NEW_TOPIC / AMBIGUOUS / REJECTED` 路由结果及层级校准、margin 和冲突检查。
- [ ] 将 checkpoint 改为：L0 局部社区 → provisional L1 → owner routing → 快速融合或正式 L1 → 逐层正常晋升。
- [ ] 快速路径失败时回退到普通 L1 路径，保证原始 segment 不丢失。
- [ ] 在 purification 中增加 owner 冲突输入，禁止明显属于不同 owner 的 L0 被写入同一个 L1。
- [ ] 为每个层级增加独立的建图阈值、resolution 和候选边策略。
- [ ] 更新 extraction/fusion prompt，使其接收 `target_level` 和该层允许保留/丢弃的信息类型。
- [ ] 增加以下测试：高层主题回归、L1/L2 重复 owner 消除、不同 owner 的 L0 拆分、快速路径失败回退、L1→L2 全局替换、无 owner 的新主题逐层晋升。
