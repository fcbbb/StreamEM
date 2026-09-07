# 语义锚点提取 Prompt v6：面向稳定记忆对象

这一版不再要求模型只生成一句“主要主题”摘要，而是先识别片段所指向的
稳定目标，再分离本段的局部方面。后续图聚类应使用 `cluster_anchor`，不要
使用 `detail_anchor` 或 `selected_anchor`。

```text
You extract a targeted semantic anchor from one conversation segment.

The downstream system does not primarily cluster sentences by topical wording.
It clusters conversation segments that belong to the same durable memory target:
the same project, document, personal practice, activity, event, capability, or
specific proposition that can be referred to and updated across conversations.

First identify the stable target. Then identify the facet discussed in this
segment. The target is the thing that persists across add/update/delete or
across different aspects. The facet is local to this segment and must not
replace the target.

Do not assume that the dataset has a fixed or known set of target types. Infer a
short role label from the segment when useful (for example "project", "habit",
"document", "event", "concept", or another concise label), but the label is
open-world and may be any short string or null. Do not force every target into a
predefined taxonomy. The target name and its boundary, not the role label, are
the primary clustering evidence.

Rules for target_name:
- Use a short canonical name for the stable target, not a sentence and not a
  complete summary.
- If the segment contains an explicit project/document/event title, preserve
  that title consistently; normalize only capitalization and surrounding
  quotes.
- Do not include the current operation, amount, date, person, or local facet in
  target_name unless it is itself the identity of the target.
- For abstract discussions, include the scope that distinguishes the target:
  for example distinguish an individual's practice from an automated system's
  capability or an organization's policy, rather than using one generic noun.
- Do not collapse all targets sharing one noun into the same generic name.

Rules for facet:
- State the aspect actually discussed in this segment.
- Keep it separate from target_name. Examples include task prioritization,
  planning versus spontaneity, motivation and well-being, or budget updates.
- It may be null only when the segment establishes a target but no meaningful
  aspect can be recovered.

Rules for the anchor strings:
- cluster_anchor must be the canonical target_name, optionally prefixed with a
  short role label only when that label is stable and genuinely disambiguates
  the target. It must omit the facet and operation.
- detail_anchor should be "<target_name> — <facet>" when facet is present, or
  just target_name otherwise.
- selected_anchor must equal detail_anchor for compatibility with older tools.
- If there is no stable, substantive target, set target_type, target_name,
  facet, cluster_anchor, detail_anchor, and selected_anchor to null.
- Return valid JSON only.

Required JSON format:
{
  "schema_version": "targeted_v2",
  "target_type": "short open-world role label or null",
  "target_name": "string or null",
  "target_scope": "one short phrase explaining the target boundary or null",
  "facet": "string or null",
  "operation": "add|update|delete|discuss|compare|plan|other or null",
  "cluster_anchor": "string or null",
  "detail_anchor": "string or null",
  "selected_anchor": "string or null",
  "reason": "one concise sentence"
}

Conversation segment:
{{conversation_segment}}
```
