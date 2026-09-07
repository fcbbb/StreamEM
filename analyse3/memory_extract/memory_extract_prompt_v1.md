# Memory Extraction Prompt v1

## 中文版

```text
请将输入的主题 group 提取为一个结构化主题记忆对象。

该 group 包含围绕同一主题的多个对话 segment。请完成以下任务：

1. 根据所有 anchor 和 segment，概括整个 group 的统一主题，填写 topic。
2. 用一段简洁的话概括该主题在对话中形成的可持续上下文，填写 summary。
3. 提取值得保留的主题上下文，填写 topic_context。主题上下文包括：具体的未解决问题、主题范围变化、设计约束、已确定的方向或下一步、主题专属结论，以及被用户接受、引用或继续使用的 AI 回答。
4. 提取用户明确表达的持续信息，填写 user_memories。用户记忆包括：用户明确表达的事实、偏好、目标、决定、计划和约束。
5. 使用对话中实际出现的内容组织摘要和记忆项，并保持原始语义范围。
6. 返回一个 JSON 对象，字段必须为 topic、summary、topic_context、user_memories。

topic_context 中每一项使用以下结构。type 是根据该内容选择的简洁标签，使用最准确的自然语言类型名称：
{
  "type": "该主题内容的类型标签",
  "content": "值得继续保留的主题上下文"
}

user_memories 中每一项使用以下结构。type 是根据该用户信息选择的简洁标签，使用最准确的自然语言类型名称：
{
  "type": "该用户信息的类型标签",
  "content": "用户明确表达的持续信息"
}

输出格式：
{
  "topic": "整个 group 的统一主题",
  "summary": "该主题形成的可持续上下文",
  "topic_context": [
    {
      "type": "open_question",
      "content": "主题中仍然需要继续讨论的问题"
    }
  ],
  "user_memories": [
    {
      "type": "goal",
      "content": "用户明确表达的目标"
    }
  ]
}
```

## English Version

```text
Extract the input topic group into one structured topic-memory object.

The group contains multiple conversation segments about a shared topic. Complete the following tasks:

1. Identify the unified topic of the entire group from all anchors and segments, and write it in topic.
2. Write a concise summary of the durable context formed in the conversation about this topic in summary.
3. Extract the topic context worth retaining in topic_context. Topic context includes specific unresolved questions, changes in topic scope, design constraints, established directions or next steps, topic-specific conclusions, and assistant answers that the user accepted, referenced, or continued to use.
4. Extract durable information explicitly expressed by the user in user_memories. User memories include explicitly expressed facts, preferences, goals, decisions, plans, and constraints.
5. Organize the summary and memory items from the conversation content and preserve its original semantic scope.
6. Return one JSON object with exactly these fields: topic, summary, topic_context, user_memories.

Each item in topic_context uses this structure:

Choose a concise, accurate natural-language type label for each item based on its content.

{
  "type": "a type label for this topic context",
  "content": "topic context worth retaining for future discussion"
}

Each item in user_memories uses this structure:

Choose a concise, accurate natural-language type label for each item based on its content.

{
  "type": "a type label for this user memory",
  "content": "durable information explicitly expressed by the user"
}

Output format:
{
  "topic": "the unified topic of the entire group",
  "summary": "the durable context formed in the conversation",
  "topic_context": [
    {
      "type": "open_question",
      "content": "a question that remains relevant to the topic"
    }
  ],
  "user_memories": [
    {
      "type": "goal",
      "content": "a goal explicitly expressed by the user"
    }
  ]
}
```
