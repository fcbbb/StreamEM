# 语义锚点提取 Prompt v2

## 适用范围

这是第二版单片段输入 Prompt，对应实验计划中的方案 C：先生成粗粒度、合适
粒度和细粒度候选，再选择一个最终语义锚点。

本版本只输入目标片段，不提供其他片段，也不提供已有锚点。相较 v1，本版本
明确优先保留用户明确表达的自身事实，避免具体用户事实被后续的模糊聊天状态覆盖。

语义锚点表示片段的主要话题，不等同于已经生成的用户记忆。一个清晰的普通
知识话题，即使不是用户个人信息，也不应仅因为“不属于用户记忆”而输出 `null`。
只有在片段没有稳定、明确的主要话题时才输出 `null`。

## 英文 Prompt

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
- If the user explicitly states a fact about their own actions, experiences,
  preferences, plans, or circumstances, treat that user-specific fact as
  substantive content and prefer it over a vague conversational state when
  choosing the main topic.
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

## 运行命令

使用仓库根目录的 `.env`，并显式指定 v2 Prompt 和 v2 输出文件：

```powershell
uv run streamem-anchor-extract --method candidate_selection_v2 --prompt F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\pilot_v1\anchor_extraction_prompt_v2.md --output F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\new_outputv2.jsonl --model gpt-5.6-luna --env-file F:\StreamEM\.env --no-proxy --resume
```

先测试 1 条：

```powershell
uv run streamem-anchor-extract --limit 1 --method candidate_selection_v2 --prompt F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\pilot_v1\anchor_extraction_prompt_v2.md --output F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\new_outputv2_test.jsonl --model gpt-5.6-luna --env-file F:\StreamEM\.env --no-proxy
```

## 推荐结果字段

API 原始结果可以保存为：

```json
{
  "segment_id": "session_0001_seg002",
  "method": "candidate_selection_v2",
  "input_variant": "single_segment",
  "coarse_candidate": "...",
  "selected_anchor": "...",
  "fine_candidate": "...",
  "reason": "...",
  "model": "...",
  "temperature": 0,
  "seed": null,
  "status": "success"
}
```

其中 `selected_anchor` 才是后续 embedding 和连边实验使用的表示。
