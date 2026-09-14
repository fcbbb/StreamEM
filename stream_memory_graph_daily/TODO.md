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
5. 社区检测图不混合任意层级。边界图可以保留相邻层的候选边，但第 `k` 层社区检测只处理第 `k` 层 peer 边；跨层归属使用独立的主题归属路由器，不把跨层边当作 Leiden 社区边。
6. 正常压缩逐层进行；快速路径允许临时 L1 直接融合到已经存在的 L2/L3 owner，但不是让原始 L0 直接越级写入高层。

### 层级和活动图

建议的逻辑层级：

```text
L0：原始事件 segment，只是输入证据和当前局部社区节点
L1：近期/局部主题记忆
L2：跨 L1 的较粗主题
L3：更长期、更宽的主题结构
```

底层保留一个短期边界图，高层使用只包含本层 memory 的 peer graph：

```text
G0 = L0 当前 segment + 尚未晋升的 L1 memory
G1 = 当前 active L1 memory peer graph
G2 = 当前 active L2 memory peer graph
G3 = 当前 active L3 memory peer graph（为后续晋升预留）
```

G0 中的 L1 是新事件快速吸收已有局部主题的稳定锚点；G1、G2、G3 的社区检测只处理对应层的 peer 边。跨层 owner 由独立 Router 决定，不通过跨层图边决定。

每个图可以保留：

- 当前层节点之间的边；
- G0 中当前层 segment 与 active L1 memory 之间的 boundary 边；
- 不在 G1/G2/G3 中混入上一层或下一层 memory；
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

raw score 只负责从活跃的 L2+ memory 中召回候选，默认取 top-k=5；最终不使用
score、margin 或候选排名直接决定归属。LLM 一次性比较全部召回候选，输出固定 schema：

```json
{
  "owner_memory_id": "xxx 或 null",
  "reason": "same_topic / new_topic / ambiguous / uncertain"
}
```

`owner_memory_id` 是唯一最终决策字段；`reason` 只用于审计。系统只接受预先定义的
四个 reason，不允许模型创造关系类别。LLM 调用失败、输出非法或没有 L2+ 候选时，
统一视为 `NO_OWNER`。

#### 阶段 C：快速晋升或普通路径

对每个临时 L1 分两条路径处理。

**快速路径：**

```text
provisional_L1 → 唯一活动 L2+ owner
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
没有唯一高层 owner / 存在歧义 / 无法判断
  → NO_OWNER
  → 按普通 L1 流程处理
  → provisional_L1 与活动 L1/L2 建立正常边
```

本阶段不把 L1 路由到 L1，也不把 L2 路由到 L3；正常晋升另行实现。

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

初版不引入抽象的 stability 分数，而以会话逻辑时间作为主要触发条件：

- `last_mentioned_at` 距 checkpoint 会话日期达到当前层的不活跃窗口；
- 活跃下层节点会作为主题上下文参与纯化，不能仅凭一条图边刷新过期节点；
- 多个候选先做局部社区检测和公共主题分组，孤立过期节点直接压缩；
- 生成的高层 memory 继承下层成员的最新 `last_mentioned_at`，每次只上移一层。

当前默认窗口为 L1=30 天、L2=90 天，作为可调配置而不是最终理论结论。

### 层级粗细与建图参数

第一版不预设每个层级使用不同的语义阈值。全局阈值表达统一的最低相似性标准，
层级差异来自被比较的表示粒度和边的语义：

```text
G0：局部事件是否同主题
G1：L1 是否具有共同的稳定主题
G2：L2 是否具有共同的长期抽象主题
```

因此，第一版先在各层复用同一个全局语义阈值，同时按层统计：

- edge density；
- average degree；
- community size；
- 跨主题误连率和候选召回率。

只有评测证明不同层的相似度分布或误连率明显不同，才引入分层阈值校准。`resolution`、
`kNN` 和候选边策略也先保持统一或按边语义设计，不把“高层更抽象”直接等同于降低阈值。

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

- [x] 将 `ActiveGraph` 泛化为按层维护的多个图：G0 保留 L0/L1 boundary，G1/G2/G3 只保留对应层 peer graph。
- [ ] 给 `MemoryRecord` 增加 `topic_lineage_id`、owner 状态和晋升版本信息；当前已实现 `level`、直接成员表示和 `last_mentioned_at`。
- [x] 将当前 `source_anchors` 的边计算逻辑推广为“直接下层表示”，避免高层每次递归扫描所有原始 segment。
- [x] 实现全局 `TopicOwnerRouter`：索引所有活动 memory；跨层候选边可以保留在边界图中，但不参与社区检测，最终 owner 仍由固定 schema 的路由调用决定。
- [ ] 实现 `OWNER / NEW_TOPIC / AMBIGUOUS / REJECTED` 路由结果及层级校准、margin 和冲突检查。
- [x] 将 checkpoint 改为：L0 局部社区 → provisional L1 → owner routing → 快速融合或普通 L1 流程；正常晋升另由时间驱动调度器处理。
- [x] 快速路径失败时回退到普通 L1 路径，保证原始 segment 不丢失。
- [x] 暂不在 purification 中增加 owner 冲突输入；当前社区纯化规则足够，后续根据评测再决定。
- [ ] 先使用全局统一语义阈值，并增加按层 edge density、degree、社区规模和误连率评测；仅在必要时再引入分层阈值校准。
- [x] 保留现有 extraction/fusion 任务 Prompt，新增公共层级策略 Prompt；由 `target_level` 注入该层的职责、必须保留、可压缩、可丢弃和禁止推断信息类型，并在调用 payload 中保留同一份结构化策略。当前原始 segment extraction 固定生成 L1，L2+ 通过同层 owner fusion 使用对应策略。
- [x] 抽取公共主题分组 Prompt；当前 purification 继续使用原有的 `memory_nodes`/`segment_nodes` 输入适配和结果校验，后续高层晋升只需替换节点输入适配器，不预先复制分层 Prompt。
- [x] 实现基于会话逻辑时间和 `last_mentioned_at` 的一层晋升：过期单节点直接压缩，多个候选先做局部社区/主题分组；高层继承下层成员最新提及时间，低层记录保留但从活动图和 owner router 移除。
- [x] 增加显式 `active_memory_ids`，将 active owner 状态从图节点布局中分离；状态恢复时兼容旧的图推断方式。
- [ ] 增加高层主题回归、L1/L2 重复 owner 消除、不同 owner 的 L0 拆分、快速路径失败回退和多节点 L1→L2 社区晋升测试。
