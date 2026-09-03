# 初版人工标注（60 个 session）

`initial_60.jsonl` 是对 `data/conversations/session_0001.json` 至
`session_0060.json` 的第一版事件切割标注，供人工审核，不是最终 gold label。

每行对应一个 session，包含：

- `units`：从原始 turn 派生的稳定语义单元，保留 `unit_id`、`message_id`、说话人和原文；
- `boundaries`：每个事件边界的 `after_unit_id` / `before_unit_id`；
- `segments`：由边界展开得到的连续事件区间；`segment_type` 直接标注为 `greeting`、`substantive` 或 `goodbye`，可用于后续过滤。

当前每个 session 默认展开为三类事件：开头寒暄、实质对话、结尾告别；如果实质对话中还有话题/任务转场，则继续产生额外事件。边界的 `initial_reason` 可直接用于过滤：`greeting_to_substantive` 和 `substantive_to_goodbye` 分别对应两类无意义内容的起止位置，`new_local_interaction_goal` 是实质对话内部的新目标边界。

切割依据与实验方案一致：右侧是否开启了可以相对独立理解的新交互目标。问答追问、澄清、补充条件、同一项目的字段收集都保留在同一事件中；明确转向活动记录、目标/偏好操作或新的独立主题时切开。`no_memory` session 若没有明确转场则标为一个事件。

当前开发集选择连续的前 60 个 session，覆盖所有 session 类型和 add/update/delete 操作。审核时直接修改 `initial_60.jsonl` 中对应行，并将 `annotation_status` 改为 `reviewed`；建议保留 `initial_reason`，另在 `review_notes` 中记录边界争议和修改理由。
