# 评测流程

评测分为两个阶段，两个阶段通过持久化的 `memory_state.json` 衔接。

## 阶段一：从对话构建记忆

```powershell
python stream_memory_graph_daily/evaluate/conversation_to_memory.py --no-proxy
```

运行时会立即显示实时进度条、当前 session、已用时间和预计剩余时间；错误会另起一行显示，不会覆盖进度。

切分和 anchor 预处理可以并发执行，但图状态仍按 session 顺序写入。首次运行可使用 4 个 worker：

```powershell
python stream_memory_graph_daily/evaluate/conversation_to_memory.py `
  --preprocess-workers 4 `
  --save-every 10 `
  --no-proxy
```

使用 `--use-cache` 后，预处理结果会保存到 `output-dir/preprocess_cache/`。缓存按源文件哈希和模型名校验，适合中断重跑或重复评测：

```powershell
python stream_memory_graph_daily/evaluate/conversation_to_memory.py `
  --preprocess-workers 4 `
  --use-cache `
  --no-proxy
```

缓存只覆盖 cutting 和 anchor；社区规划、记忆抽取/融合仍会在有状态的顺序流程中执行。

默认读取：

```text
stream_memory_graph_daily/evaluate/data/conversations/session_*.json
```

如果 `--conversation-directory` 指向一个包含多个 persona 子目录的目录，
脚本会自动逐个处理子目录，并把每个 persona 的状态隔离保存。例如：

```powershell
python stream_memory_graph_daily/evaluate/conversation_to_memory.py `
  --conversation-directory F:\StreamEM\Memora\data\weekly `
  --output-dir stream_memory_graph_daily/evaluate/artifacts/weekly_memory `
  --use-cache `
  --no-proxy
```

输出会变成 `artifacts/weekly/<persona>/memory_state.json`，不会把不同
persona 的对话合并到同一张记忆图中。

默认写入：

```text
stream_memory_graph_daily/evaluate/artifacts/memory_build/
├── memory_state.json
├── memories.jsonl
├── segments.jsonl
├── boundaries.jsonl
├── trace.jsonl
├── stage_audit.jsonl
├── share_memory_labels.jsonl
├── share_memory_attribution.jsonl
├── share_memory_report.json
├── progress.json
└── run_manifest.json
```

`share_memory_labels.jsonl` 是独立的评估监督 sidecar。构建脚本在调用记忆管线前会移除消息中的 `share_memory` 以及 session 级 `operation`/`operation_details`；这些字段不会进入切分、anchor、社区、抽取或融合请求。`share_memory_attribution.jsonl` 只在运行后建立 message → unit → segment → memory 的结构映射，`share_memory_report.json` 报告 anchor-null、boundary、no-memory、compressed 等阶段结果。这里的 compressed 只是结构状态，不代表内容已经被正确保留。

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

`questions_file` 也可以直接传入包含多个 persona 的目录。脚本会按文件名
`evaluation_questions_<persona>.json` 自动匹配对应的
`<state-root>/<persona>/memory_state.json`，并将结果写入
`<output-dir>/<persona>/`：

```powershell
python stream_memory_graph_daily/evaluate/memory_to_answer.py `
  F:\StreamEM\Memora\data\weekly `
  --state-file stream_memory_graph_daily/evaluate/artifacts/weekly_memory `
  --output-dir stream_memory_graph_daily/evaluate/artifacts/weekly_evaluation `
  --no-proxy
```

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

## 离线评估 share_memory 内容保留

结构归因完成后，可单独运行语义保留评估：

```powershell
python stream_memory_graph_daily/evaluate/share_memory_to_report.py `
  --state-file stream_memory_graph_daily/evaluate/artifacts/memory_build/memory_state.json `
  --labels-file stream_memory_graph_daily/evaluate/artifacts/memory_build/share_memory_labels.jsonl `
  --no-proxy
```

该命令在记忆构建完成后才读取监督 sidecar，对 add、update、delete 分别判断目标状态是否被正确表达，并生成：

```text
share_memory_semantic_results.json
share_memory_semantic_report.json
```

它是独立 evaluator，不会把监督信息回流到记忆图或修改已有 state。正式实验应在冻结 prompt 和配置后使用未参与调优的数据运行该评估。

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





命令：
uv run .\stream_memory_graph_daily\evaluate\conversation_to_memory.py `
  --conversation-directory .\Memora\data\monthly\academic_researcher\conversations `
  --output-dir .\stream_memory_graph_daily\evaluate\artifacts\monthly_memory\academic_researcher `
  --preprocess-workers 16  --postprocess-workers 16`
  --save-every 10 `
  --use-cache `
  --no-proxy