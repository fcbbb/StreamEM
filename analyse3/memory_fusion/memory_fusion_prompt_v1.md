# Memory Fusion Prompt v1

## 中文版

```text
你负责维护一个特定话题的结构化记忆。请将 new_group 中的新对话融合到 existing_memory 中，返回融合后的完整 topic、完整 summary，以及 topic_context 和 user_memories 所需的增量操作。

先理解 existing_memory 表达的完整当前状态，再判断新对话带来的持久新增、补充、修正、状态变化或问题解决。operations 是产生最终状态所需的最小变更集；topic 和 summary 是在 existing_memory 上应用这些变更后的完整最终值。

## 字段与保留标准

- topic：根据融合后的完整记忆，写出一个简洁、稳定、能够概括整个记忆对象的主题名称。主题范围保持稳定；新对话确实扩展或改变主题范围时再更新名称。
- summary：用 1～2 句话概括融合后的完整主题讨论了什么，以及目前形成了什么状态。摘要聚焦主要子方面和当前状态。新内容改变主题内容或状态时，生成完整的新摘要。
- topic_context：记录会影响未来继续讨论该主题的持久上下文，包括用户仍然希望继续解决的具体问题、当前有效的项目或任务状态、决定、约束、计划、下一步、用户明确提出的后续要求，以及用户已经接受或继续使用的必要主题结论。
- user_memories：完整记录用户明确表达的用户事实和持续信息，包括事实、经历、偏好、目标、决定、计划、约束、具体金额、日期、数量、测量值、进展、状态和行动。

topic_context 采用实际影响标准：保留会改变未来回答、决定或行动的内容。一般性知识、背景解释、普通问答、兴趣展开和仅服务于当轮交流的用户提问保留在原始对话中；当它们改变主题整体讨论范围时，可在 summary 中概括。topic_context 可以使用空数组表示当前缺少符合保留标准的内容。

user_memories 以用户的明确陈述为依据。不同事实分别记录，并保留原始具体信息；完全重复的事实合并为一项，不同时间、不同数值或不同事件的事实分别保留。每条记录事实本身。用户提问用于识别主题方向、任务或开放问题；AI 回答在被用户接受、被后续引用或形成项目专属结论时进入 topic_context。

整理要求：

- topic_context 合并重复或高度相似的内容；
- user_memories 合并完全重复的事实，保留不同时间或不同数值的独立事实；
- 每个 content 使用简洁的一句话，并保留用户事实中的具体信息；
- type 使用准确、简洁的自然语言标签，标签采用开放集合；
- 每项输出内容都能追溯到 existing_memory 或 new_group。

## 融合原则

1. 将新信息加入最合适的字段，并与等价或高度重合的旧条目合并。
2. 对同一事项的补充、推进、修正或状态变化，使用 update 返回融合后的完整条目。
3. 对已经解决的问题，更新或删除原开放问题条目，并将对未来有价值的结论加入或合并到相应条目。
4. 新对话聚焦其他内容时，继续保留仍然有效的旧信息。
5. 所有新增和修改都以 existing_memory 或 new_group 中的明确依据为准。
6. 新旧信息存在冲突且当前证据支持多种解释时，保留双方说法、时间差异或不确定性；证据仅支持原状态时维持现有记忆。
7. 寒暄、措辞性内容、临时反应、重复表达、普通问答、泛泛知识、AI 单方表达和缺少对话依据的推断维持现有记忆状态。
8. operations 采用能够产生最终状态的最少操作，合并针对同一条目的连续变化。

## 操作

### add

新增一项与现有条目具有独立信息价值的 topic_context 或 user_memories。

- field 使用 topic_context 或 user_memories；
- id 使用产生该内容的 segment_id；
- value 为完整的新内容：{"type": "类型", "content": "新增内容"}。

### update

融合已有 topic_context 或 user_memories 条目的补充、推进、修正或状态变化。

- field 使用原条目所在字段 topic_context 或 user_memories；
- id 使用该条目的 item_id；
- value 为完整的更新后内容：{"type": "类型", "content": "更新后的完整内容"}。

### delete

清除已经明确失效、取消、被完整替代、被解决或适用范围已经终止的已有 topic_context 或 user_memories 条目。

- field 使用原条目所在字段 topic_context 或 user_memories；
- id 使用该条目的 item_id；
- 条目仍有有效部分时，使用一次 update 保留有效内容。

## 输出格式

输出一个合法 JSON 对象，字段固定为 topic、summary 和 operations：

{
  "topic": "融合后的完整 topic",
  "summary": "融合后的完整 summary",
  "operations": [
    {
      "operation": "add|update|delete",
      "field": "topic_context|user_memories",
      "id": "操作目标 ID",
      "value": "add 和 update 的完整内容"
    }
  ]
}

topic 和 summary 始终返回融合后的完整值；内容保持时复用 existing_memory 中的原值。operations 承载 topic_context 和 user_memories 的实际变化：add 使用对应 segment_id，update 和 delete 使用对应 item_id。add 和 update 操作由 operation、field、id、value 四个字段构成；delete 操作由 operation、field、id 三个字段构成。每个 ID 直接复用输入中的 ID。增量为空时，operations 返回空数组。最终响应采用纯 JSON。
```

## English Version

```text
You maintain structured memory for one specific topic. Fuse the new conversation in new_group into existing_memory. Return the complete fused topic and summary together with the incremental operations required for topic_context and user_memories.

First understand the complete current state represented by existing_memory. Then identify durable additions, elaborations, corrections, state changes, or resolutions introduced by the new conversation. operations is the smallest change set that produces the final state. topic and summary are the complete final values after applying those changes to existing_memory.

## Fields and retention criteria

- topic: From the complete fused memory, write one concise and stable topic name that represents the entire memory object. Keep the scope stable and update the name when the new conversation materially expands or changes that scope.
- summary: In 1–2 sentences, summarize what the complete fused topic covers and the state it has reached. Focus the summary on the main aspects and current state. Produce a complete new summary when the new content changes topic content or state.
- topic_context: Record durable context that can affect future discussion of the topic, including a specific issue the user still wants to resolve, a current project or task state, decision, constraint, plan, next step, explicit follow-up requirement, or necessary topic conclusion that the user accepted or continued to use.
- user_memories: Preserve all user facts and durable information explicitly expressed by the user, including facts, experiences, preferences, goals, decisions, plans, constraints, specific amounts, dates, counts, measurements, progress, states, and actions.

Apply a material-impact test to topic_context: retain content that can change a future answer, decision, or action about the topic. Keep general knowledge, background explanations, ordinary Q&A, casual exploration, and user questions serving the current turn in the source conversation; summarize them when they change the topic's overall coverage. An empty array represents a topic_context with zero qualifying items.

Base user_memories on explicit user statements. Record distinct facts separately and preserve their original concrete details. Merge exact duplicates, while keeping facts with different times, values, or events separate. Each item records the fact itself. Use user questions to identify topic direction, tasks, or open issues. Place assistant answers in topic_context when the user accepts them, later dialogue relies on them, or they form a project-specific conclusion.

Organization requirements:

- Merge repeated or highly similar topic_context content;
- Merge exact duplicate user facts and preserve independent facts with different times or values;
- Write each content value as one concise sentence while preserving concrete details from user facts;
- Use an accurate and concise natural-language type label from an open set;
- Ground every output item in existing_memory or new_group.

## Fusion rules

1. Put new information in the most appropriate field and merge it with equivalent or highly overlapping existing content.
2. For an elaboration, advance, correction, or state change to the same matter, use update to return the complete fused item.
3. For a resolved issue, update or delete the former open-question item and add or merge any durable conclusion into the appropriate item.
4. Preserve valid existing information when the new conversation focuses elsewhere.
5. Ground every addition and revision in explicit evidence from existing_memory or new_group.
6. When old and new information conflict and the current evidence supports multiple interpretations, preserve both claims, their temporal difference, or the uncertainty. Keep the current memory state when the evidence supports the existing state alone.
7. Keep the current memory state for greetings, wording-only content, transient reactions, repeated expressions, ordinary Q&A, general knowledge, assistant-only statements, and inferences lacking conversational evidence.
8. Use the fewest operations that produce the final state and combine successive changes to the same item.

## Operations

### add

Add a topic_context or user_memories item that carries distinct information value relative to the existing items.

- field is topic_context or user_memories;
- id is the segment_id that produced the content;
- value is the complete new content: {"type": "type", "content": "new content"}.

### update

Fuse an elaboration, advance, correction, or state change into an existing topic_context or user_memories item.

- field is the original item's field, topic_context or user_memories;
- id is that item's item_id;
- value is the complete updated content: {"type": "type", "content": "complete updated content"}.

### delete

Remove an existing topic_context or user_memories item that is explicitly invalidated, cancelled, fully replaced, resolved, or has reached the end of its applicability.

- field is the original item's field, topic_context or user_memories;
- id is that item's item_id;
- when part of the item remains valid, use one update to preserve the valid content.

## Output format

Return one valid JSON object with exactly the fields topic, summary, and operations:

{
  "topic": "complete fused topic",
  "summary": "complete fused summary",
  "operations": [
    {
      "operation": "add|update|delete",
      "field": "topic_context|user_memories",
      "id": "operation target ID",
      "value": "complete content for add and update"
    }
  ]
}

Always return the complete fused topic and summary; reuse their existing_memory values when their content remains stable. operations carries actual changes to topic_context and user_memories: add uses the corresponding segment_id, while update and delete use the corresponding item_id. An add or update object contains operation, field, id, and value; a delete object contains operation, field, and id. Reuse every ID directly from the input. Return an empty operations array when the incremental change set is empty. Format the final response as pure JSON.
```
