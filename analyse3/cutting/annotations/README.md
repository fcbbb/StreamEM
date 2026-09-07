# 人工审计标注（60 个 session）

`initial_60.jsonl` 是对 `data/conversations/session_0001.json` 至
`session_0060.json` 的完整规则重标版本，仍不是最终多标注者 gold label。

每行对应一个 session，包含：

- `units`：从原始 turn 派生的稳定语义单元，保留 `unit_id`、`message_id`、说话人和原文；
- `boundaries`：每个事件边界的 `after_unit_id` / `before_unit_id`；
- `segments`：由边界展开得到的连续事件区间；`segment_type` 直接标注为 `greeting`、`substantive` 或 `goodbye`，可用于后续过滤。

当前每个 session 默认展开为三类事件：开头寒暄、实质对话、结尾告别；如果实质对话中还有话题/任务转场，则继续产生额外事件。边界的 `initial_reason` 可直接用于过滤：`greeting_to_substantive` 和 `substantive_to_goodbye` 分别对应两类无意义内容的起止位置，`new_local_interaction_goal` 是实质对话内部的新目标边界。

切割依据与实验方案一致：右侧是否开启了可以相对独立理解的新交互目标。问答追问、澄清、补充条件、同一项目的字段收集都保留在同一事件中；明确转向活动记录、目标/偏好操作或新的独立主题时切开。`no_memory` session 若没有明确转场则标为一个事件。

当前开发集选择连续的前 60 个 session，覆盖所有 session 类型和 add/update/delete 操作。本次完整重标的核心原则是事实级纯度：事实内容要完整且单一；同一事实或局部目标下的助手附和、追问和回答可以保留在同一事件中；只有出现另一个独立事实或目标时才切开。goodbye 是否单独切开不是核心判据，只要不破坏事实完整性即可。对于助手问题与用户回答，不在两者之间切开；但用户明确转入独立事实时，事实仍从用户事实起点单独切开。`initial_reason` 保留用于说明边界来源。
