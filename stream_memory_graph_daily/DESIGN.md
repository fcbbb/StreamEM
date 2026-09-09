# 实现设计说明

## 每日生命周期

segment 必须按照 `event_date` 非递减顺序流式写入。当较晚日期的第一个 segment 到达时，管线会先对已经完整累积的前一个日期执行 checkpoint，然后才允许新日期的 segment 进入图中。流结束时，使用 `finalize()` 处理最后一个尚未结束的日期。

完整对话进入管线时，切割模型先产生一个窗口内的全部连续 segment，随后锚点模型通过一次批量请求为这些 segment 分别生成 anchor。输出必须与输入 `segment_id` 一一对应且保持顺序；任一 ID 缺失、重复或虚构都会使该窗口整体重试，避免部分写入。

第一次 checkpoint 会处理首批活动节点。后续 checkpoint 只检查当天新增批次能够触达的图连通区域，因此不会重新处理完全无关的 committed memory。

## 活动图

活动图当前只包含两类节点：

- `segment`：使用提取出的语义 anchor 作为图表示；
- `memory`：以结构化记忆的 `topic` 为主表示，并保存已压缩成员的历史 anchor 作为简化超节点成员表示。

图存储为无向图，但采用在线增量 kNN：新 `segment` 到达时，只计算它与当前活动 `segment`/`memory` 的相似度，并保留其阈值化 top-k 邻居；新建或更新 `memory` 时，只计算它与当前活动 `segment` 的相似度。已有节点不会因为新节点到达而重新筛选 top-k。状态恢复时才使用全量重建来从快照节点恢复边。系统分别配置 `segment-segment` 和 `segment-memory` 的相似度阈值，并直接跳过所有 `memory-memory` 节点对。

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

`memory.topic` 是 memory 的稳定语义身份，历史成员 anchor 只能作为共识支持，不能替换 topic 作为 base。成员按 `source_segments` 一一取回 anchor 并分别计票，即使多个 segment 的 anchor 文本相同也不会被去重。成员共识计算复用现有 `new_memory_threshold`，不引入额外超参数。当前仍是一层图：历史成员不恢复为活动节点，聚合计算只形成一条 `memory-segment` 边。

社区成功压缩后，原始证据仍保存在 `SegmentRecord` 以及 memory 的来源字段中，但不再作为活动图节点参与下一轮社区检测。

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
- 当前活动图节点和边；
- boundary 及其候选 memory 得分；
- pending segment；
- cannot-link memory 对；
- checkpoint 和 LLM 错误记录；
- 预留的 memory 关系存储。

恢复状态时，系统根据活动 segment anchor、memory topic 以及 memory 的历史来源 anchor 重新生成向量并重建超节点边，同时检查 memory 记录与活动 memory 节点是否一致。旧状态中没有超节点成员表示时，会从 `MemoryRecord.source_anchors` 自动补齐。

## 记忆关系预留

`MemoryRelationStore` 已经提供关系对象、序列化和恢复接口，但当前默认关闭：

- 不允许写入 memory-memory 关系；
- 不创建 memory-memory 图边；
- 不参与当前单层图的社区检测；
- 后续实现上层 memory graph 时可以在不改变底层活动图数据结构的情况下启用。
