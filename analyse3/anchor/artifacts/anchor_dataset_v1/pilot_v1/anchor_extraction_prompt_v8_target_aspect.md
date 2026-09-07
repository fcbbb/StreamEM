# 语义锚点提取 Prompt v8

```text
You need to extract a semantic anchor from the conversation segment below.

The anchor has two parts:

- core_target: the stable subject or relation that the segment is mainly about;
- aspect: the specific matter discussed about that core_target in this segment.

The core_target should identify the enduring subject of the conversation, not a
temporary action, incidental detail, or isolated named entity. The aspect should
capture the concrete dimension discussed in the segment.

The result must be:

- representative of the segment's main content;
- specific enough to distinguish closely related subjects;
- neither overly broad nor limited to a subordinate detail;
- understandable without the original conversation.

Use concise noun phrases. Do not write a summary, sentence, or keyword list. If
the segment has no clear main subject, set both fields to null. Return valid JSON
only and do not include any additional fields.

Conversation segment:
{{conversation_segment}}

Required JSON format:
{
  "core_target": "string or null",
  "aspect": "string or null"
}
```

