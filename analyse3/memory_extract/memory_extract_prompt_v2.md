# Memory Extraction Prompt v2 — Balanced Retention

## 中文版

```text
请将输入的主题 group 压缩为一个紧凑但完整的结构化主题记忆对象。

目标是减少逐条复述，同时保留未来继续讨论该主题所需要的主要内容。记忆是对原始对话的有损压缩，不是逐段对话记录。

请完成以下任务：

1. 根据整个 group 确定一个简洁、稳定的统一主题，填写 topic。
2. 用 1～2 句描述该主题当前的主要状态和重要内容，填写 summary。
3. 选择有未来复用价值的主题上下文，填写 topic_context。保留未解决问题、当前约束、已确定决定、进行中的方向、必要的主题结论，以及有助于理解主题发展的主要子方面。
4. 选择有未来复用价值且由用户明确表达的持续信息，填写 user_memories，例如用户明确表达的事实、偏好、目标、决定、计划或约束。
5. 合并重复或高度相似的内容，保留彼此独立且有持续价值的内容；不设置固定条数上限，但避免逐条复述所有对话细节。
6. 每个 content 使用简洁表述，保留必要的对象、状态、条件或时间信息。
7. 为每项选择一个准确、简洁的自然语言 type 标签。type 标签由内容决定，不使用固定枚举。
8. 使用对话中实际出现的内容完成压缩，并保持原始语义范围。
9. 返回一个 JSON 对象，字段必须为 topic、summary、topic_context、user_memories。

判断一项内容是否值得保留时，使用以下标准：

- 如果未来继续讨论该主题时，缺少这项内容会导致理解、回答方向、决策或行动发生明显变化，则保留；
- 如果这项内容只是背景知识、普通解释、示例、重复表达或没有后续作用的问答，则省略；
- 如果数字、时间、进展或具体例子构成主题的状态、目标、约束或重要变化，则保留；
- 用户问题只有在形成具体未解决问题、约束、决定或下一步时保留；
- AI 回答只有在被用户接受、引用、继续使用，或构成当前主题的必要结论时保留；
- user_memories 只记录用户明确表达且未来可复用的信息，不根据提问次数、语气或 AI 回答推测用户属性；
- 相同内容只保留一次，选择最简洁的表述。

topic_context 中每一项使用以下结构：
{
  "type": "根据内容生成的类型标签",
  "content": "一项最小且可复用的主题上下文"
}

user_memories 中每一项使用以下结构：
{
  "type": "根据内容生成的类型标签",
  "content": "一项用户明确表达的最小且可复用信息"
}

输出格式：
{
  "topic": "统一主题",
  "summary": "主题当前最重要的持续状态",
  "topic_context": [],
  "user_memories": []
}
```

## English Version

```text
Compress the input topic group into a compact but complete structured topic-memory object.

The goal is to reduce line-by-line repetition while preserving the main content needed for future discussion of the topic. The memory is a lossy compression of the conversation, not a transcript.

Complete the following tasks:

1. Identify one concise and stable unified topic for the entire group, and write it in topic.
2. Describe the main durable state and important content of the topic in 1–2 concise sentences, and write it in summary.
3. Select the topic context with future reuse value, and write it in topic_context. Retain unresolved questions, current constraints, established decisions, active directions, necessary topic conclusions, and major independent aspects that help explain how the topic developed.
4. Select durable information with future reuse value that was explicitly expressed by the user, and write it in user_memories. Examples include explicitly expressed user facts, preferences, goals, decisions, plans, or constraints.
5. Merge repeated or highly similar content and preserve independent content with continuing value. Use a compact set of items without imposing a fixed item limit, and avoid restating every dialogue detail.
6. Write each content value as one concise statement containing the necessary object, state, condition, or time information.
7. Choose one accurate and concise natural-language type label for each item. The type label is determined by the content and comes from an open set of labels.
8. Perform the compression from the actual conversation content and preserve its semantic scope.
9. Return one JSON object with exactly these fields: topic, summary, topic_context, user_memories.

Use the following retention test for each candidate item:

- Retain it when omitting it could materially change understanding, direction, decision, or action in a future discussion of the topic;
- Omit background knowledge, generic explanations, examples, repeated statements, and questions or answers with no continuing role;
- Retain numbers, dates, progress, or concrete examples when they define the topic's state, goal, constraint, or important change;
- Retain a user question when it creates a specific unresolved issue, constraint, decision, or next step;
- Retain an assistant answer when the user accepted, referenced, or continued to use it, or when it is a necessary conclusion for the current topic;
- Use user_memories only for explicit, reusable user information. Derive them from explicit user statements rather than question frequency, tone, or assistant content;
- Represent duplicate content once, using the shortest accurate wording.

Each item in topic_context uses this structure:
{
  "type": "a concise type label chosen from the content",
  "content": "one minimal and reusable piece of topic context"
}

Each item in user_memories uses this structure:
{
  "type": "a concise type label chosen from the content",
  "content": "one minimal and reusable piece of explicitly stated user information"
}

Output format:
{
  "topic": "the unified topic",
  "summary": "the single most important durable state of the topic",
  "topic_context": [],
  "user_memories": []
}
```
