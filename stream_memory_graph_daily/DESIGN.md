# 实现设计说明

## 每日生命周期

segment 必须按照 `event_date` 非递减顺序流式写入。当较晚日期的第一个 segment 到达时，管线会先对已经完整累积的前一个日期执行 checkpoint，然后才允许新日期的 segment 进入图中。流结束时，使用 `finalize()` 处理最后一个尚未结束的日期。

完整对话进入管线时，切割模型先产生一个窗口内的全部连续 segment，随后锚点模型通过一次批量请求为这些 segment 分别生成 anchor。输出必须与输入 `segment_id` 一一对应且保持顺序；任一 ID 缺失、重复或虚构都会使该窗口整体重试，避免部分写入。

第一次 checkpoint 会处理首批活动节点。后续 checkpoint 只检查当天新增批次能够触达的图连通区域，因此不会重新处理完全无关的 committed memory。

checkpoint 内部先串行完成社区检测，再并发执行相互独立的社区纯化请求；纯化结果确定后，再并发执行各 purified group 的 memory extraction/fusion 请求。所有 LLM 结果、审计事件、图更新和记忆状态变更都在主线程按 planned group 的稳定顺序提交，失败时仍按社区或 purified group 粒度进入 retry。

## 活动图

活动图当前只包含两类节点：

- `segment`：使用提取出的语义 anchor 作为图表示；
- `memory`：以结构化记忆的 `topic` 为主表示，并保存已压缩成员的历史 anchor 作为简化超节点成员表示。

图存储为无向图，但采用在线增量 kNN：新 `segment` 到达时，只计算它与当前活动 `segment`/`memory` 的相似度，并保留其阈值化 top-k 邻居；新建或更新 `memory` 时，只计算它与当前活动节点的相似度。已有节点不会因为新节点到达而重新筛选 top-k。状态恢复时才使用全量重建来从快照节点恢复边。相似度阈值仍由全局配置统一控制；边是否参与社区检测则由节点层级和边语义决定。

memory 超节点与活动 segment 的边权定义为：

```text
weight(memory, segment)
  = base
    + (1 - base) × coverage × support_strength

base
  = similarity(memory.topic, segment.anchor)

coverage
  = 达到 new_memory_threshold 的历史成员数量 / 历史成员总数

support_strength
  = 达到阈值的历史成员相似度平均值
```

`memory.topic` 是 memory 的稳定语义身份，历史成员 anchor 只能作为共识支持，不能替换 topic 作为 base。成员按 `source_segments` 一一取回 anchor 并分别计票，即使多个 segment 的 anchor 文本相同也不会被去重。成员共识计算复用现有 `new_memory_threshold`，不引入额外超参数。历史成员不恢复为活动节点，聚合计算只形成当前活动节点之间的边。

社区成功压缩后，原始证据仍保存在 `SegmentRecord` 以及 memory 的来源字段中，但不再作为活动图节点参与下一轮社区检测。

### 短期工作区与长期整理区

G0 是在线短期工作区，不只是一个普通的同层社区图。它同时包含当前
L0 segment 和 active L1 memory：L0 segment 之间进行局部社区检测，L0 与
L1 之间的 boundary 边则让新事件能够快速吸收到已有的局部主题。L1 在这里
作为持续的局部主题锚点，避免只根据当前批次的少量 segment 生成不稳定的新主题。

G1、G2 等高层图承担长期记忆的整理和晋升，不需要混入上一层 memory。当前
管线使用以下图布局：

```text
G0：L0 segment + active L1 memory，短期吸收和普通 L1 更新
G1：active L1 memory peer graph，L1 → L2 的同层社区检测
G2：active L2 memory peer graph，L2 → L3 的同层社区检测
G3：active L3 memory peer graph，预留给后续更高层晋升
```

因此，社区检测始终只读取本层 peer 边。G0 中的 L1 不是为了参加 L0 的
同层聚类，而是作为已有主题的实时归属入口；这正是底层和高层职责不对称
的原因。高层跨层 owner 判断由 `TopicOwnerRouter` 完成，不依赖 G1/G2
中的跨层图边或图连通性。

## 分层图的边语义

分层图中的“相似”只表示候选关系，不表示所有节点都应该参加同一种社区运算。系统区分三类边：

- `peer`：同层节点之间的相似边，用于同层社区检测、主题整合和逐层晋升；
- `boundary`：L0 segment 与活动 L1 memory 之间的边，用于新事件实时吸收到已有局部主题；
- `cross_level`：如果旧状态或底层图 API 中存在相邻层 memory 候选边，只能作为兼容性/审计信息，不能把不同层级的节点放入同一个社区；当前管线的跨层 owner 判断由独立 Router 完成。

因此，G0 保留 L0↔L1 的 boundary 边，因为新事件必须能够找到已有 L1 的普通更新入口；G1、G2、G3 只保存对应层的 memory peer 边。跨层 owner 的最终判断仍由 `TopicOwnerRouter` 和固定 JSON schema 完成，不能由 raw score、图连通性或跨层路径直接决定。

这样可以避免 `L1-A ↔ L2 ↔ L1-B` 的传递连接把两个本来不同的 L1 主题错误合并。相似度阈值在各层复用同一全局标准；不同层的不同含义来自表示粒度和边的生命周期职责，而不是人为设置不同阈值。

## 多 memory 社区的分配

在调用记忆 LLM 之前，系统会检查包含多个 memory 的连通分量。对其中每个新 segment，直接计算它与该社区内每个 memory 的相似度，并分配给相似度最高的 memory；不使用跨 segment 的路径支持、最低支持度或 margin boundary 判断。相似度相同时按 memory ID 稳定打破平局。

分配完成后，每个实际收到 segment 的 memory 形成一个独立的 purification 输入组。没有收到新 segment 的 memory 不进入本轮 purification、fusion 或 extraction，保持不变。这样传给 `CommunityPurifier` 的每个组最多包含一个已有 memory。

## 社区纯化

分配完成后，尚未归档的每个 planned community 会进入 `CommunityPurifier`。纯化输入同时包含该组中的已有 memory 节点和新 segment 节点；memory 提供 topic、summary、结构化内容和历史 anchor，segment 提供 anchor 与原文。模型需要判断 memory 与新 segment 是否有自然且有信息量的共同父主题，而不是因为存在图边就强制融合。

只有完全不包含已有 memory、且只有一个新 segment 的新主题 singleton 才跳过 LLM。只要 community 包含已有 memory，即使只有一个新 segment，也必须执行纯化判断。

纯化可以把新 segment 与已有 memory 分开：不含 memory 的 group 进入新的 memory extraction；只含已有 memory、不含新 segment 的 group 表示保留原 memory 不变；同时包含 memory 和 segment 的 group 才进入 memory fusion。若纯化确实把一个 community 拆成多个 group，其中某个 group 只有一个新 segment，则该 singleton 只保留在 active graph 中，跳过 extraction/fusion，等待后续新 segment 提供更多社区证据；它不会被归档为 `no_memory`，也不会从图中删除。

纯化拆分后会立即删除不同 purified group 之间原有的 memory-segment 和 segment-segment 边，节点本身仍保留。这样旧图边不会在下一轮增量社区检测中把模型刚拆开的 group 重新连回去；未来新 segment 仍可通过新的增量边重新建立合理连接。

纯化调用方严格验证：

- 输出只能包含 `groups`；
- 每个 group 只能包含 `group_id` 和 `node_ids`；
- 每个输入 memory/segment `node_id` 必须且只能出现一次；
- 不允许删除、生成或重复 memory/segment 节点。

纯化结果会写入 `stage_audit` 的 `llm_call`、`normalized_result` 和 `applied` 事件，并在 `trace` 中写入紧凑的 `community_purified` 事件；拆分产生的跨 group 边删除会单独记录为 `edges_cut`。每个 purified group 随后根据其节点组成独立处理：无 memory 且包含多个 segment 的 group 进入 memory extraction；有一个 memory 且包含 segment 的 group 进入 memory fusion；memory-only group 保持原 memory 不变；拆分产生的单 segment group 仅记录 `skipped_singleton` 并保持 active。

纯化失败时，原 community 的 segment 全部保留 active 并进入 retry；纯化成功但某个 group 的 memory 操作失败时，只重试该 group 的 segment。

## 记忆写入规则

### 不包含 memory 的社区

调用 `memory_extraction.txt`：

- 返回 `{}`：把已经审阅的证据归档为 `no_memory`；
- 返回有效结构化记忆：创建新的稳定 memory 节点，并用其 `topic` 替换原始社区在活动图中的表示。

### 包含一个 memory 的社区

调用 `memory_fusion.txt`。调用方负责：

- 保持 `memory_id` 不变；
- `add` 不接受模型生成的 ID，由代码根据 memory、字段、内容和来源生成独立稳定的 `item_id`；
- 验证 `update/delete` 只引用已有条目的 `item_id`，不允许把 `segment_id` 当作条目身份；
- 验证每项操作的 `source_segment_ids` 均来自本轮 `new_group`，并将其写入持久化 trace；
- 当 `topic` 或 `summary` 发生变化时，由代码把本轮完整 `new_group` 记录为该整体字段的来源；
- 按顺序原子应用最小操作集，并拒绝重复或不存在的目标；
- 由调用方补齐本轮已经审阅的 anchor 和 segment 来源；
- 保存能够直接作为下一轮输入的完整 memory 状态；
- 使用融合后的 `topic` 更新活动图中的 memory 表示。

### 包含多个 memory 的原始社区

原始社区可以包含多个 memory，但在进入记忆 LLM 前会按 segment 的最高 memory 相似度拆成多个 purification 组。因此记忆 extraction/fusion Prompt 不会收到包含多个已有 memory 的输入。

## 状态与恢复

状态文件保存：

- 所有原始 segment 及其处理状态；
- 所有结构化 memory；
- `active_memory_ids`：当前有效主题 owner 的显式集合；低层 memory 被高层替换后仍保留在 memory store，但从该集合移除；
- 当前活动图节点和边；
- boundary 及其候选 memory 得分；
- pending segment；
- cannot-link memory 对；
- checkpoint 和 LLM 错误记录；
- 预留的 memory 关系存储。

恢复状态时，系统先恢复 `active_memory_ids`，再按当前层布局规范化活动图：G0 保留 active L1 boundary 节点，G1/G2/G3 分别只保留对应层的 peer 节点。旧状态没有 `active_memory_ids` 时，先从旧活动图节点推断一次。随后根据活动 segment anchor、memory topic 以及 memory 的直接成员表示重新生成向量并重建边，同时检查 memory 记录与活动 memory 节点是否一致。旧状态中没有超节点成员表示时，会从 `MemoryRecord.source_anchors` 自动补齐。

`active_memory_ids` 将“历史上是否保存过 memory”“当前是否是有效 owner”和“是否需要参加某张社区图”分开。图只负责组织和晋升，memory store 负责保存历史，Router 和检索都以该集合为 active 来源。

## 检索与回答

问题检索默认只查询两类 active 节点：

- `active_memory_ids` 对应的所有层级 memory，使用 topic、summary、结构化内容和来源 anchor；
- G0 中仍未压缩的 active segment，使用 anchor 和原文。

检索将语义相似度、BM25 词法匹配和实体匹配通过 RRF 合并，返回 top-k。它不沿社区图做路径遍历，也不直接查询已经从 active 集合移除的低层 memory 或已经压缩的原始 segment。回答 LLM 只接收检索得到的 memory 结构化内容和 active segment 内容，并被要求仅依据这些内容回答。原始 segment 和历史低层 memory 仍在状态文件中保存，后续如需恢复被高层压缩掉的细节，再增加独立的历史证据 fallback retrieval。

## 记忆关系预留

`MemoryRelationStore` 已经提供关系对象、序列化和恢复接口，但当前默认关闭：

- 不允许写入 memory-memory 关系；
- 运行时可以保留相邻层 memory 的 `cross_level` 候选边，但不把它们当作 memory-memory peer 关系；
- 同层 memory-memory peer 边只在对应层的社区视图中参与检测；
- owner 归属仍由独立路由器决定，不由关系存储或图连通性直接决定。





L0 segment
  → L0 社区检测与纯化
  → 形成 provisional L1
  → 旁路默认关闭；高层候选仅作为后续再激活设计的预留上下文
  → 统一走普通 L1 创建或融合
  → 后续 checkpoint 按 last_mentioned_at 独立检查时间晋升
      → L1 → L2
      → L2 → L3
