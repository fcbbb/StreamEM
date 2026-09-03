# 语义锚点评测数据集 v1

本目录基于筛选后的 `substantive_segments.jsonl` 构建，原始切割结果来自
`cutting_opencode_go_v5_all`。

- 筛选后的实质片段：277 个
- 问题种子组：15 个
- 问题文件涉及的会话：73 个
- 候选会话对：1,576 对
- 基于启发式标签识别出的 AI 相关实质片段：88 个

## 已落盘文件

- `segments.jsonl`：277 个评测片段及其来源信息。
- `segment_gold_labels.jsonl`：277 个片段的英文语义标注结果。
- `pair_candidates.jsonl`：1,576 个候选片段对。
- `pair_gold_labels.jsonl`：1,576 个片段对的英文预标注结果。
- `annotation_schema.json`：标注字段和允许取值。
- `experiment_manifest.json`：数据集构建清单。

## 分批结果

片段标注同时保存在 `direct_annotations/` 下的 14 个批次文件中。

片段对标注保存在 `direct_annotations/pair_labels/` 下的 79 个批次文件中，
每批最多 20 对，最后一批有 16 对。

片段标注是直接英文预标注。片段对标注是根据片段标注中的英文主题描述进行的、
可复现的规则辅助预标注，不是外部 API 生成的结果。在作为最终 gold label 使用前，
建议进行双人独立复核或双盲人工仲裁。

筛选后的 `substantive_segments.jsonl` 是评测集成员资格的权威来源，其中包括已经
人工复核保留的边界例外片段。

`heuristic_tags` 和 `candidate_types` 仅用于候选检索和组织，不应直接当作主题 gold
label，也不应直接作为最终语义锚点。
