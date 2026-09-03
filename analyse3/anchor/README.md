# 语义锚点实验

本目录包含语义锚点评测集构建、pilot 采样、人工标注辅助和 LLM 锚点提取工具。所有数据路径均相对于当前仓库的 `analyse3/anchor` 目录解析，API 环境文件默认使用仓库根目录的 `.env`。

## 使用 uv 运行

```bash
# 从切割结果和评测问题构建候选数据集
uv run streamem-anchor-build

# 从候选数据集中选择固定的 60 条 pilot 样本
uv run streamem-anchor-pilot

# 根据片段标签生成 pair 标注（会写入 direct_annotations/）
uv run streamem-anchor-annotate

# 运行锚点提取（默认读取 pilot_v1）
uv run streamem-anchor-extract --help
uv run streamem-anchor-extract --limit 1
```

也可以使用模块入口，例如：

```bash
uv run python -m analyse3.anchor.run_anchor_extraction --help
```

锚点提取默认读取仓库根目录 `.env` 中的 `CUTTING_OPENAI_API_KEY`、`GRAPH_WEEKLY_OPENAI_API_KEY`、`OPENAI_API_KEY` 或 `OPENROUTER_API_KEY`，也可以通过 `--env-file`、`--api-key` 和 `--base-url` 覆盖。

`build` 和 `pilot` 会写入 `artifacts/anchor_dataset_v1` 下的候选文件；`extract` 会调用 API 并写入 JSONL，请在正式运行前确认输入和输出参数。
