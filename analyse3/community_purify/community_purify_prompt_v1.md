# Community Purification Prompt v1

下面的中文和英文内容表达同一套规则。实际调用时可以使用其中一种语言；如果使用双语版本，则保留两部分。

## 中文 Prompt

```text
你需要对一个语义社区中的 segment 进行保守拆分。

输入包含若干 segment。每个 segment 有唯一的 segment_id、anchor，以及可选的原文 text。

如果所有 segment 都能用一个自然、有信息量的共同父主题概括，则保留。不同的子主题或具体方面，只要仍然属于这个共同父主题，就不需要拆分。只有当找不到这样的共同父主题时，才拆分。不确定时保留。

这里的“自然、有信息量”是指：
- 这个主题是这些 segment 真正共享的语义，而不是临时拼出的集合；
- 主题能够指出共同的对象或语义范围，但不需要区分其中的每个子主题；
- 主题不能只是一个没有具体语义的泛称。

优先依据 anchor 判断；只有当 anchor 不清楚或与 text 明显不一致时，才使用 text 进行核对。

输出严格 JSON：
{
  "groups": [
    {
      "group_id": "g1",
      "segment_ids": ["segment_id_1", "segment_id_2"]
    }
  ]
}

要求：
- 不需要拆分时，只输出一个包含全部 segment 的 group；
- 需要拆分时，输出多个 group；
- 输出只能包含分组结构、group_id 和分组后的 segment_id；
- 每个 segment_id 必须且只能出现一次；
- 不得删除或生成 segment_id。
```

## English Prompt

```text
Conservatively split the segments in one semantic community.

The input contains several segments. Each segment has a unique segment_id, an anchor, and optionally the original text.

Keep the community if all segments can be described by one natural and informative common parent topic. Different subtopics or specific aspects do not require splitting as long as they still belong to that common parent topic. Split only when no such common parent topic exists. When uncertain, keep the community.

“Natural and informative” means that:
- the topic reflects a real semantic commonality among the segments rather than an ad hoc collection;
- it identifies a common object or semantic scope, but does not need to distinguish every subtopic;
- it is not a label with no concrete semantic content.

Use the anchors as the primary evidence. Use the text only when an anchor is unclear or clearly inconsistent with the text.

Output valid JSON only:
{
  "groups": [
    {
      "group_id": "g1",
      "segment_ids": ["segment_id_1", "segment_id_2"]
    }
  ]
}

Requirements:
- If no split is needed, output one group containing all segments;
- If a split is needed, output multiple groups;
- The output may contain only the group structure, group_id values, and grouped segment_id values;
- Each segment_id must appear exactly once;
- Do not delete segments or invent segment_id values.
```
