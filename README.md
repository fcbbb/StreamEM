# StreamEM

本项目包含对话事件切割实验和流式图记忆实验。依赖由 `uv` 管理，项目内路径均相对于仓库根目录解析。

## 创建环境

```bash
uv sync
```

如果需要运行图记忆的真实向量编码、Leiden 社区发现、KeyBERT 或 spaCy NER，再安装可选图依赖：

```bash
uv sync --extra graph
```

## 运行对话切割

```bash
uv run streamem-cutting run --dry-run
uv run streamem-cutting run --session-ids 1,2 --output-dir analyse3/cutting/artifacts/cutting_smoke
```

也可以使用模块入口：

```bash
uv run python -m analyse3.cutting.cutting_experiment run --dry-run
```

API 凭据从仓库根目录的 `.env` 读取，也可以通过 `--env-file`、`--api-key` 和 `--base-url` 覆盖。不要把 `.env` 提交到版本库。

## 运行图记忆

```bash
uv run streamem-graph-weekly --help
```

图记忆的编码模型通过 `--encoder-model` 或 `ENCODER_MODEL_NAME` 配置，默认使用模型名 `all-MiniLM-L6-v2`；未安装或无法加载模型时会使用确定性的哈希编码降级实现。
