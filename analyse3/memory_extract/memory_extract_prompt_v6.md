# Memory Extraction Prompt v6 — Sparse Context, Complete User Facts

## 中文版

```text
请从输入的一个纯化主题 group 中提取一个结构化主题记忆对象。

输出需要表达：

1. 这个 group 的统一主题；
2. 这个主题在对话中讨论的主要内容和形成的状态；
3. 会影响未来继续讨论该主题的少量持久上下文；
4. 用户在对话中明确表达的全部用户事实和持续信息。

字段要求：

- topic：根据整个 group 的 anchor 和对话内容，写出一个简洁、稳定、能够概括整个 group 的主题名称。
- summary：用 1～2 句话概括这个主题在对话中讨论了什么，以及主题目前形成了什么状态。可以概括主要子方面，但不需要逐条复述普通问答。
- topic_context：只记录会影响未来继续讨论该主题的持久上下文。包括用户仍然希望继续解决的具体问题、当前有效的项目或任务状态、决定、约束、计划、下一步、用户明确提出的后续要求，以及用户已经接受或继续使用的必要主题结论。
- user_memories：完整记录用户明确表达的用户事实和持续信息，包括事实、偏好、目标、决定、计划、约束、具体金额、日期、数量、测量值、进展、状态和行动。

topic_context 的保留标准：如果删除一项内容，未来对该主题的回答、决定或行动不会发生实际变化，就不保留这项内容。一般性知识、背景解释、普通问答、兴趣展开和没有形成任务或状态的用户提问只用于概括 summary，不进入 topic_context。没有符合条件的内容时，topic_context 返回空数组。

user_memories 中，用户明确表达的事实和经历都应详细保留。不同事实分别记录，保留其原始具体信息，不合并成范围或概括；每条只记录事实本身，不添加额外解释。用户提问本身不等于用户记忆，AI 的回答也不自动成为用户记忆。

整理要求：

- topic_context 合并重复或高度相似的内容；
- user_memories 只合并完全重复的事实，保留不同时间或不同数值的独立事实；
- 每个 content 使用简洁的一句话，但不得删除用户事实中的具体信息；
- 为每项内容生成一个准确、简洁的自然语言 type 标签，type 不使用固定枚举；
- 只根据输入对话内容提取和概括。

输出一个 JSON 对象，字段必须为 topic、summary、topic_context、user_memories。

topic_context 中每项格式为：
{
  "type": "根据内容生成的类型标签",
  "content": "会影响未来主题讨论的持久上下文"
}

user_memories 中每项格式为：
{
  "type": "根据内容生成的类型标签",
  "content": "用户明确表达的完整事实或持续信息"
}

输出格式：
{
  "topic": "统一主题",
  "summary": "主题讨论内容和当前状态的简要概括",
  "topic_context": [],
  "user_memories": []
}

特殊情况：如果 topic_context 和 user_memories 都为空，说明没有值得保留的记忆内容。此时允许直接返回空 JSON 对象 {}，不填写 topic 和 summary。
```

## English Version

```text
Extract one structured topic-memory object from the input purified topic group.

The output should represent:

1. the unified topic of the group;
2. the main content discussed and the state reached in the conversation;
3. a small amount of durable context that can affect future discussion of the topic;
4. all user facts and durable information explicitly expressed by the user.

Field requirements:

- topic: Use all anchors and conversation content to write one concise and stable topic name that represents the entire group.
- summary: Summarize in 1–2 sentences what the conversation discussed about the topic and what state the topic reached. The summary may mention the main aspects, but it does not need to restate ordinary question-and-answer content.
- topic_context: Include only durable context that can affect future discussion of the topic. This includes a specific issue the user still wants to resolve, a current project or task state, decision, constraint, plan, next step, explicit follow-up requirement, or necessary topic conclusion that the user accepted or continued to use.
- user_memories: Preserve all user facts and durable information explicitly expressed by the user, including facts, preferences, goals, decisions, plans, constraints, specific amounts, dates, counts, measurements, progress, states, and actions.

Retention rule for topic_context: if removing an item would not materially change a future answer, decision, or action about the topic, leave it out of topic_context. General knowledge, background explanation, ordinary question-and-answer content, casual topic exploration, and user questions that create no task or state belong in summary rather than topic_context. Return an empty topic_context array when no item meets this condition.

In user_memories, preserve all facts and experiences explicitly stated by the user in detail. Record distinct facts separately, retain their original concrete information, and do not merge them into ranges or generalizations. Write only each fact itself, without extra explanation. A user question by itself is not a user memory, and an assistant answer does not automatically become a user memory.

Organization requirements:

- Merge repeated or highly similar topic_context items;
- Merge user_memories only when the facts are exact duplicates; preserve independent facts with different times or values;
- Write each content value as one concise sentence without removing concrete information from a user fact;
- Choose one accurate and concise natural-language type label for each item; type labels come from an open set;
- Extract and summarize only from the input conversation.

Return one JSON object with exactly these fields: topic, summary, topic_context, user_memories.

Each topic_context item uses:
{
  "type": "a type label chosen from the content",
  "content": "durable context that can affect future discussion of the topic"
}

Each user_memories item uses:
{
  "type": "a type label chosen from the content",
  "content": "a complete fact or durable piece of information explicitly stated by the user"
}

Output format:
{
  "topic": "the unified topic",
  "summary": "a concise summary of the discussion and current topic state",
  "topic_context": [],
  "user_memories": []
}

Special case: when both topic_context and user_memories are empty, the group contains no retainable memory content. In that case, return the empty JSON object {} without topic or summary.
```
