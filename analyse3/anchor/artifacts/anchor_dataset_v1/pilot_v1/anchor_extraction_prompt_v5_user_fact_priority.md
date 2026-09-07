# 语义锚点提取 Prompt v5：用户事实优先

本版本完整保留原 Prompt，仅增加了 `Additional evidence-priority instruction`
这一段约束。

```text
You need to extract one semantic anchor from a conversation segment between a
user and an LLM.

A semantic anchor is a stable topic expression that represents what the
segment is mainly about. It is not a full summary, a list of keywords, a single
incidental entity, or a complete event description.

The anchor should satisfy three requirements:

1. Representativeness: it captures the main content of the segment rather than
   a greeting, transition, incidental detail, or background entity.
2. Distinctiveness: it is specific enough to distinguish this topic from
   closely related but different topics.
3. Granularity: it should represent the segment's main topic at an appropriate
   level of abstraction—neither so broad that it subsumes multiple distinct
   topics nor so narrow that it captures only a subordinate detail.
4. Standalone interpretability: a reader who has not seen the conversation
   should understand what the anchor refers to and what aspect is being
   discussed without relying on omitted context.

First propose three candidates:

- coarse_candidate: a deliberately broader topic expression;
- selected_anchor: the best final semantic anchor;
- fine_candidate: a deliberately more specific expression.

Then select the best candidate as selected_anchor. The final anchor should
prefer the form "specific object + specific aspect" when appropriate.

Important instructions:

- Output exactly one final anchor, or null only if the segment does not contain
  substantive content that establishes a clear main topic.
- Do not make the anchor more specific than the evidence in the segment.
- Do not treat an incidental amount, date, person, tool, or closing phrase as
  the main topic unless it is the actual subject of the segment.
- Express each candidate as concisely as possible while preserving all
  information necessary for a complete and standalone understanding.
- If selected_anchor is null, set coarse_candidate and fine_candidate to null.
- Return valid JSON only. Do not include Markdown fences or additional text.

Required JSON format:
{
  "coarse_candidate": "string or null",
  "selected_anchor": "string or null",
  "fine_candidate": "string or null",
  "reason": "one concise sentence explaining the choice"
}

Conversation segment:
{{conversation_segment}}
```
