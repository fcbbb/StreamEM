# Runs

每个子目录代表一次不可混淆的实验运行，推荐结构为：

```text
runs/<prompt-version>/<run-id>/
  predictions.jsonl
  run_manifest.json
  evaluation.json
  evaluation.md
  purity_review.jsonl       # 可选
```

不要用模型名或临时描述覆盖 Prompt 版本。Prompt 迭代时建立新的 `<prompt-version>`，同一 Prompt 的重复实验使用新的 `<run-id>`。
