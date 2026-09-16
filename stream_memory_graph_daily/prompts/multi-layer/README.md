# Multi-layer prompt source

This directory is the single source of runtime prompts for the daily memory
graph.

- `common/` contains prompts whose semantic task does not depend on memory
  level.
- `templates/` contains one extraction template and one fusion template.
  Input-mode instructions and level rules are injected by the loader.
- `levels/` contains only the semantic retention rules that differ between
  L1, L2, and L3.

The runtime prompt is composed as:

```text
common semantics + task template + input-mode rules + one level rule block
```

There is intentionally no separate prompt file for `extraction_from_memories`,
`fusion_from_l1`, or `community_purification`. Those are input modes or legacy
names, not different semantic tasks.
