# Topic annotations

`topic_annotations.jsonl` 是对 `community_inputs.jsonl` 的人工语义标注。

- `single_topic: true`：社区内所有 segment 可以用一个自然、具体且信息量足够的共同主题概括。
- `single_topic: false`：社区需要按 `groups` 拆分；每个 segment 必须且只出现一次。
- `groups[*].topic`：该组的主题描述。
- `groups[*].segment_ids`：属于该主题的 segment。
- `reason`：简要判断依据。

标注采用主题级标准：需要有明确共享的领域和讨论对象，但不要求具体实例完全相同。消费记录与预算目标、不同演员的偏好、计划与完成状态等，只要仍服务于同一个明确主题，就保留在同一组。只有当 segment 的主要对象或讨论层级明显不同，合并才会造成主题混杂。
