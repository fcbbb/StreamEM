# StreamEM 评测运行命令

在 `F:\StreamEM` 目录下执行以下命令。命令中已经显式写出所有需要传值的可选参数；布尔开关和互斥选项见后面的说明。

## 1. 对话转换为每日记忆图

```powershell
python .\stream_memory_graph_daily\evaluate\conversation_to_memory.py `
  --conversation-directory .\stream_memory_graph_daily\evaluate\data\conversations `
  --output-dir .\stream_memory_graph_daily\evaluate\artifacts\memory_build `
  --state-file .\stream_memory_graph_daily\evaluate\artifacts\memory_build\memory_state.json `
  --progress-file .\stream_memory_graph_daily\evaluate\artifacts\memory_build\progress.json `
  --limit 100 `
  --session-range 1-100 `
  --save-every 1 `
  --preprocess-workers 1 `
  --postprocess-workers 1 `
  --cache-dir .\stream_memory_graph_daily\evaluate\artifacts\memory_build\preprocess_cache `
  --llm-model gpt-5.6-luna `
  --api-key $env:LOCAL_OPENAI_API_KEY `
  --base-url $env:LOCAL_OPENAI_BASE_URL `
  --env-file .\.env `
  --use-proxy `
  --timeout 120 `
  --max-tokens 4096 `
  --encoder-model all-MiniLM-L6-v2 `
  --device cpu `
  --knn-k 10 `
  --new-new-threshold 0.5 `
  --new-memory-threshold 0.5 `
  --assignment-min-support 0.35 `
  --assignment-margin 0.08
```

可选开关：

```powershell
# 断点续跑；需要已有 --state-file 和 --progress-file
--resume

# 启用 session 预处理缓存
--use-cache

# 遇到第一个错误就立即退出
--fail-fast

# 不使用 LLM（会覆盖 LLM 调用；不要与需要 LLM 抽取的流程混用）
--no-llm
```

`--use-proxy` 与 `--no-proxy` 二选一；上面的命令使用 `--use-proxy`，如需关闭代理，将其替换为 `--no-proxy`。`--api-key` 和 `--base-url` 也可以省略，脚本会从 `.env` 或环境变量读取。

## 2. 使用记忆图回答并评测问题

```powershell
python .\stream_memory_graph_daily\evaluate\memory_to_answer.py `
  .\stream_memory_graph_daily\evaluate\data\evaluation_questions_academic_researcher.json `
  --state-file .\stream_memory_graph_daily\evaluate\artifacts\memory_build\memory_state.json `
  --output-dir .\stream_memory_graph_daily\evaluate\artifacts\evaluation `
  --limit 100 `
  --task-type Remembering `
  --task-type Reasoning `
  --task-type Recommending `
  --top-k 5 `
  --judge-workers 1 `
  --question-workers 4 `
  --answer-model gpt-5.6-luna `
  --judge-model gpt-5.6-luna `
  --api-key $env:LOCAL_OPENAI_API_KEY `
  --base-url $env:LOCAL_OPENAI_BASE_URL `
  --env-file .\.env `
  --use-proxy `
  --timeout 120 `
  --answer-max-tokens 3000 `
  --judge-max-tokens 1000 `
  --encoder-model all-MiniLM-L6-v2 `
  --device cpu
```

可选开关：

```powershell
# 从已有 evaluation_results.json 继续执行
--resume

# 遇到第一个问题错误就立即退出
--fail-fast

# 只做记忆检索，不调用回答模型
--retrieval-only

# 生成回答但跳过 Judge 评测
--skip-judge
```

`--task-type` 可以重复使用；如果不指定，则评测全部任务类型。`--retrieval-only` 与 `--skip-judge` 可以按需使用，但使用 `--retrieval-only` 时不会调用回答模型和 Judge。`--use-proxy` 与 `--no-proxy` 二选一。

## 常用简化命令

如果使用脚本默认路径，可以省略全部路径和默认值：

```powershell
python .\stream_memory_graph_daily\evaluate\conversation_to_memory.py
python .\stream_memory_graph_daily\evaluate\memory_to_answer.py
```
