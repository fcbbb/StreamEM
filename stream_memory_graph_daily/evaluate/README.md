# 评测流程

评测分为两个阶段，两个阶段通过持久化的 `memory_state.json` 衔接。

## 阶段一：从对话构建记忆

```powershell
python stream_memory_graph_daily/evaluate/conversation_to_memory.py --no-proxy
```

运行时会立即显示实时进度条、当前 session、已用时间和预计剩余时间；错误会另起一行显示，不会覆盖进度。

默认读取：

```text
stream_memory_graph_daily/evaluate/data/conversations/session_*.json
```

默认写入：

```text
stream_memory_graph_daily/evaluate/artifacts/memory_build/
├── memory_state.json
├── memories.jsonl
├── segments.jsonl
├── boundaries.jsonl
├── trace.jsonl
├── progress.json
└── run_manifest.json
```

`memory_state.json` 是第二阶段使用的完整可恢复状态。处理过程中默认每完成一个 session 就保存一次，因此中断后可以使用 `--resume` 继续：

```powershell
python stream_memory_graph_daily/evaluate/conversation_to_memory.py --no-proxy --resume
```

执行少量数据冒烟测试：

```powershell
python stream_memory_graph_daily/evaluate/conversation_to_memory.py `
  --limit 5 `
  --no-proxy `
  --output-dir stream_memory_graph_daily/evaluate/artifacts/smoke
```

## 阶段二：检索记忆、回答并评测

```powershell
python stream_memory_graph_daily/evaluate/memory_to_answer.py --no-proxy
```

运行时会显示当前问题、完成比例、已用时间和预计剩余时间。进度信息写入标准错误流，最终 JSON 汇总仍保持在标准输出流。

默认读取：

- `data/evaluation_questions_academic_researcher.json`
- `artifacts/memory_build/memory_state.json`

默认生成：

```text
stream_memory_graph_daily/evaluate/artifacts/evaluation/
├── evaluation_results.json
└── evaluation_report.json
```

`evaluation_results.json` 保存每道问题检索到的结构化记忆、回答、逐项 judge 结果和单题 FAMA。`evaluation_report.json` 保存总体指标以及 Remembering、Reasoning、Recommending 三类任务的分项指标。

只检查检索结果、不调用回答及 judge 模型：

```powershell
python stream_memory_graph_daily/evaluate/memory_to_answer.py `
  --retrieval-only `
  --limit 5
```

生成回答但跳过 judge：

```powershell
python stream_memory_graph_daily/evaluate/memory_to_answer.py `
  --skip-judge `
  --no-proxy `
  --limit 5
```

恢复被中断的正式评测：

```powershell
python stream_memory_graph_daily/evaluate/memory_to_answer.py --no-proxy --resume
```

构建记忆和执行检索时必须使用相同的 `--encoder-model`，否则相似度结果不可比较。

## 模型与接口配置

默认使用以下模型：

- 记忆构建、答案生成、Judge：`gpt-5.6-luna`
- 向量编码：`all-MiniLM-L6-v2`

两个评测脚本默认读取仓库根目录的 `.env`，并优先使用：

```text
LOCAL_OPENAI_API_KEY
LOCAL_OPENAI_BASE_URL
```

`LOCAL_OPENAI_BASE_URL` 可以填写完整的 `/chat/completions` 地址，客户端会自动转换为 API 根地址。

HTTP 客户端默认读取系统代理环境变量。连接本机或不需要代理的兼容接口时添加 `--no-proxy`；该参数会让回答、Judge 以及记忆构建中的全部 LLM 请求使用 `trust_env=False`。
