# 语义锚点提取 Prompt v1.1

## 适用范围

这是第一版 Prompt 的输出表达规范版本，对应实验计划中的方案 C：先生成
粗粒度、合适粒度和细粒度候选，再选择一个最终语义锚点。

本版本只输入目标片段，不提供其他片段，也不提供已有锚点。这样可以作为
后续参考段消融实验的单片段基线。

语义锚点表示片段的主要话题，不等同于已经生成的用户记忆。一个清晰的普通
知识话题，即使不是用户个人信息，也不应仅因为“不属于用户记忆”而输出 `null`。
只有在片段没有稳定、明确的主要话题时才输出 `null`。

## 英文 Prompt

```text
You need to extract one semantic anchor from a conversation segment between a
user and an LLM.

A semantic anchor is a stable topic expression that represents what the
segment is mainly about.

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

Then select the best candidate as selected_anchor. Whenever the segment
contains a stable object or topic and a specific aspect, selected_anchor must
use the canonical form:

<stable object or topic> — <specific aspect>

The stable object or topic must come first, and the specific aspect must come
after the em dash. The expression should be a concise noun phrase, not a
complete sentence. Preserve enough information in the stable object or topic
to distinguish it from closely related subjects; do not replace it with a
generic label. If no specific aspect is present, use only the stable object or
topic.

Important instructions:

- Output exactly one final anchor, or null only if the segment does not contain
  substantive content that establishes a clear main topic.
- Do not make the anchor more specific than the evidence in the segment.
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

## 调用建议

- 对 `pilot_sample_60.jsonl` 的 60 个片段逐条调用。
- 每次调用只替换 `{{conversation_segment}}`，不要把 gold reference 一起传给模型。
- 使用结构化输出或 JSON Schema，避免返回非 JSON 文本。
- 保存模型名称、模型版本、temperature、seed、请求时间和原始响应。
- 第一轮建议固定 temperature；后续稳定性实验再重复调用或改写输入。

## 推荐结果字段

API 原始结果可以保存为：

```json
{
  "segment_id": "session_0001_seg002",
  "method": "candidate_selection_v1_1",
  "input_variant": "single_segment",
  "coarse_candidate": "...",
  "selected_anchor": "stable object or topic — specific aspect",
  "fine_candidate": "...",
  "reason": "...",
  "model": "...",
  "temperature": 0,
  "seed": null,
  "status": "success"
}
```

其中 `selected_anchor` 才是后续 embedding 和连边实验使用的表示。

