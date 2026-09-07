# Memory extraction experiment

这个实验框架用于比较不同记忆提取 prompt 对纯化主题 group 的提取质量。

## 输入

默认读取：

```text
analyse3/community_purify/artifacts/two_days_v1/
├── community_inputs.jsonl
└── purified_communities.jsonl
```

每个纯化后的 group 被视为一个主题记忆对象的输入样本。

## 运行提取

先做离线结构验证：

```powershell
python -m analyse3.memory_extract.experiment --dry-run
```

使用模型运行：

```powershell
python -m analyse3.memory_extract.experiment `
  --prompt analyse3/memory_extract/memory_extract_prompt_v1.md `
  --output-dir analyse3/memory_extract/artifacts/two_days_v1
```

输出：

```text
memory_candidates.jsonl  每个 group 的记忆提取结果
evaluation.json          自动结构指标
run_manifest.json        本次实验配置
```

支持 `--resume` 从已有 JSONL 继续运行。

## 自动指标

当前自动检查：

- JSON 和核心字段是否有效；
- source anchors 和 segment IDs 是否由程序完整生成；
- 各种运行状态和错误数量。

自然语言摘要不使用字符串精确匹配评价，因为同一个主题可以有多个正确表述。

模型只生成 `topic`、`summary`、`topic_context` 和 `user_memories`。`source_anchors` 与 `source_segments` 是由实验脚本根据输入 group 自动补齐的确定性字段。

## 人工评估

生成评估模板：

```powershell
python -m analyse3.memory_extract.experiment `
  --make-annotation-template `
  --output-dir analyse3/memory_extract/artifacts/two_days_v1
```

在 `annotation_template.jsonl` 中为每个候选填写 1～5 分：

- `topic`：主题是否准确、是否抓住共同父主题；
- `faithfulness`：摘要和保留内容是否忠实于输入；
- `retention_precision`：保留下来的内容中，是否主要是值得长期保留的内容；
- `retention_recall`：重要的主题上下文和用户记忆是否被保留；
- `user_memory_grounding`：`user_memories` 是否确实来自用户的明确表达。

评分含义：`1=差，3=可接受，5=优秀`。

填写后重新运行：

```powershell
python -m analyse3.memory_extract.experiment `
  --resume `
  --annotations analyse3/memory_extract/artifacts/two_days_v1/annotation_template.jsonl `
  --output-dir analyse3/memory_extract/artifacts/two_days_v1
```

人工评分汇总会写入 `evaluation.json`。

提取阶段的结果使用 `case_id`（`community_id::group_id`）标识输入样本；最终稳定的 `memory_id` 留给后续记忆融合阶段生成。这样不会让模型在每次实验中自行生成不可复用的记忆 ID。

## 比较 prompt

为不同 prompt 使用不同的 `--output-dir`，并对相同输入样本分别生成 annotation 文件。例如：

```text
artifacts/two_days_v1_prompt_a/
artifacts/two_days_v1_prompt_b/
```

最后比较各自 `evaluation.json` 中的自动指标和五项人工评分。
