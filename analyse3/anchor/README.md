# 语义锚点实验

本目录包含语义锚点评测集构建、pilot 采样、人工标注辅助和 LLM 锚点提取工具。所有数据路径均相对于当前仓库的 `analyse3/anchor` 目录解析，API 环境文件默认使用仓库根目录的 `.env`。

本轮实验的最终结论见：[anchor_experiment_conclusions_v1.md](anchor_experiment_conclusions_v1.md)。

## 运行前准备

在 PowerShell 中从仓库根目录执行：

```powershell
Set-Location F:\StreamEM
uv sync
```

锚点提取使用 OpenAI Python SDK 的 Chat Completions 接口，请先启动本地兼容服务，并确认下面的地址可访问：

```text
http://localhost:8080/v1/chat/completions
```

SDK 的 `base_url` 需要填写 API 根地址 `http://localhost:8080/v1`，SDK 会自动补上 `/chat/completions`。脚本默认已经使用该地址；也可以显式传入完整地址，脚本会自动规范化。脚本默认读取 `F:\StreamEM\.env` 中的本地服务配置：

```text
LOCAL_OPENAI_API_KEY=<your-local-api-key>
LOCAL_OPENAI_BASE_URL=http://localhost:8080/v1/chat/completions
```

请直接使用 `.env` 中已经配置的 key，不要把真实 key 写入 README 或提交到版本库。

## 最简单的运行方法

如果你只想马上测试锚点提取，只需要做下面 3 件事：

1. 先启动你的本地模型服务，并确认它监听 `http://localhost:8080`。
2. 打开 PowerShell，复制执行下面两行：

```powershell
Set-Location F:\StreamEM
uv run streamem-anchor-extract --limit 1 --model gpt-5.6-luna --env-file F:\StreamEM\.env --no-proxy
```

这里 `--limit 1` 表示只处理 1 条，用来测试连接；`--model gpt-5.6-luna` 是模型名；`--env-file F:\StreamEM\.env` 表示从 `.env` 读取地址和 key；`--no-proxy` 表示不经过代理访问 localhost。

测试成功后，复制执行下面这一行处理全部 60 条：

```powershell
uv run streamem-anchor-extract --model gpt-5.6-luna --env-file F:\StreamEM\.env --no-proxy --resume
```

其中 `--resume` 表示中途停止后再次运行时跳过已经成功的片段。正常情况下不需要手动填写 URL 或 API key，脚本会从 `F:\StreamEM\.env` 读取：

```text
LOCAL_OPENAI_BASE_URL=http://localhost:8080/v1/chat/completions
LOCAL_OPENAI_API_KEY=你的本地服务 key
```

如果只想重新测试 1 条，把完整运行命令中的 `--resume` 换成 `--limit 1` 即可。

## 运行全量 277 条

`pilot_sample_60.jsonl` 只有 60 条；全量输入文件是：

```text
F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\segments.jsonl
```

全量提取会调用本地模型约 277 次。请保持本地服务运行，然后执行下面一行；结果写入新的 `new_outputv1_full.jsonl`，不会覆盖 60 条的 v1 结果：

```powershell
uv run streamem-anchor-extract --method candidate_selection_v1 --input F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\segments.jsonl --prompt F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\pilot_v1\anchor_extraction_prompt_v1.md --output F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\new_outputv1_full.jsonl --model gpt-5.6-luna --env-file F:\StreamEM\.env --no-proxy --resume
```

全量提取完成后，使用全量的 1,576 个 pair 进行评测：

```powershell
uv run streamem-anchor-eval --method "v1=analyse3/anchor/artifacts/anchor_dataset_v1/output/new_outputv1_full.jsonl" --pairs "analyse3/anchor/artifacts/anchor_dataset_v1/pair_candidates.jsonl" --gold "analyse3/anchor/artifacts/anchor_dataset_v1/pair_gold_labels.jsonl" --segment-gold "analyse3/anchor/artifacts/anchor_dataset_v1/segment_gold_labels.jsonl" --embedding-model all-MiniLM-L6-v2 --precision-target 0.95 --out-dir "analyse3/anchor/artifacts/anchor_dataset_v1/output/full_v1_eval"
```

全量评测结果主要查看：

```text
F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\full_v1_eval\summary.md
F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\full_v1_eval\v1\pair_scores.md
```

如果中途停止，再次执行第一条命令即可；`--resume` 会跳过已经成功的片段。不要用 `--limit 1` 或 `--limit 60`，否则仍然只会处理部分数据。

## 更改输入、Prompt 和输出

在命令后面添加对应参数即可：

```powershell
uv run streamem-anchor-extract --input "F:\StreamEM\my_input.jsonl" --prompt "F:\StreamEM\my_prompt.md" --output "F:\StreamEM\my_output.jsonl" --model gpt-5.6-luna --env-file F:\StreamEM\.env --no-proxy
```

三个参数的含义是：

```text
--input   要读取的输入 JSONL 文件
--prompt  要使用的 Prompt Markdown 文件
--output  要写入的结果 JSONL 文件
```

例如，只更换输入文件：

```powershell
uv run streamem-anchor-extract --input "F:\StreamEM\new_input.jsonl" --model gpt-5.6-luna --env-file F:\StreamEM\.env --no-proxy
```

只更换输出文件：

```powershell
uv run streamem-anchor-extract --output "F:\StreamEM\new_output.jsonl" --model gpt-5.6-luna --env-file F:\StreamEM\.env --no-proxy
```

路径中有空格时必须使用双引号。使用 `--resume` 时，程序会读取指定的输出文件并跳过其中已经成功的片段；不使用 `--resume` 时，指定的输出文件可能被覆盖。

## 最简单的评测方法

必须先完成全部 60 条提取，不能只用 `--limit 1` 的测试文件。然后在 `F:\StreamEM` 的 PowerShell 中执行：

```powershell
uv sync --extra anchor-eval
uv run streamem-anchor-eval --method "v1=analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/anchor_outputs_candidate_selection_v1.jsonl" --pairs "analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/pilot_pair_candidates.jsonl" --gold "analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/pilot_pair_gold_labels.jsonl" --segment-gold "analyse3/anchor/artifacts/anchor_dataset_v1/segment_gold_labels.jsonl" --embedding-model all-MiniLM-L6-v2 --precision-target 0.95 --out-dir "analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/anchor_similarity_eval_v1"
```

这里 `v1=` 后面是要评测的锚点输出文件。评测结果会写入：

```text
F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\pilot_v1\anchor_similarity_eval_v1\summary.md
```

主要查看 `summary.md`；首次评测时 `all-MiniLM-L6-v2` 模型可能需要下载。

## 完整运行流程

以下命令均在 `F:\StreamEM` 执行。首次运行按顺序执行构建、抽样和标注；数据已经生成时，可直接从“锚点提取”开始。

### 1. 构建候选数据集

```powershell
uv run streamem-anchor-build
```

输出写入 `analyse3/anchor/artifacts/anchor_dataset_v1/`。需要查看参数时：

```powershell
uv run streamem-anchor-build --help
```

### 2. 固定 pilot 样本

```powershell
uv run streamem-anchor-pilot
```

默认生成 60 条 pilot 输入，文件为：

```text
analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/pilot_sample_60.jsonl
```

查看抽样命令参数：

```powershell
uv run streamem-anchor-pilot --help
```

### 3. 生成 pair 标注辅助文件

```powershell
uv run streamem-anchor-annotate
```

该步骤不调用 API，会在数据集目录下写入 direct annotations 相关文件。查看参数：

```powershell
uv run streamem-anchor-annotate --help
```

### 4. 先运行 1 条进行连通性验证

```powershell
uv run streamem-anchor-extract `
  --limit 1 `
  --model gpt-5.6-luna `
  --env-file F:\StreamEM\.env `
  --no-proxy
```

成功后，检查输出：

```powershell
Get-Content analyse3\anchor\artifacts\anchor_dataset_v1\pilot_v1\anchor_outputs_candidate_selection_v1.jsonl
```

如果本地服务不支持 `response_format: {"type":"json_object"}`，追加 `--no-json-mode`：

```powershell
uv run streamem-anchor-extract --limit 1 --model gpt-5.6-luna --env-file F:\StreamEM\.env --no-proxy --no-json-mode
```

### 5. 提取全部 pilot 结果

```powershell
uv run streamem-anchor-extract `
  --model gpt-5.6-luna `
  --env-file F:\StreamEM\.env `
  --no-proxy `
  --resume
```

`--resume` 会复用输出 JSONL 中已经成功的 `segment_id`，适合网络中断或服务重启后继续运行。默认输入、prompt 和输出分别为：

```text
输入：analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/pilot_sample_60.jsonl
Prompt：analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/anchor_extraction_prompt_v1.md
输出：analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/anchor_outputs_candidate_selection_v1.jsonl
```

### 6. 常用提取参数

```powershell
# 指定输入、prompt 和输出文件
uv run streamem-anchor-extract `
  --input analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/pilot_sample_60.jsonl `
  --prompt analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/anchor_extraction_prompt_v1.md `
  --output analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/anchor_outputs_candidate_selection_v1.jsonl `
  --model gpt-5.6-luna `
  --env-file F:\StreamEM\.env --no-proxy --resume

# 只处理指定片段
uv run streamem-anchor-extract `
  --segment-id <segment_id> `
  --model gpt-5.6-luna `
  --env-file F:\StreamEM\.env --no-proxy

# 调整超时、输出长度或请求条数
uv run streamem-anchor-extract `
  --limit 10 `
  --model gpt-5.6-luna `
  --request-timeout 300 `
  --max-output-tokens 4096 `
  --env-file F:\StreamEM\.env --no-proxy

# 查看所有参数
uv run streamem-anchor-extract --help
```

也可以使用模块入口：

```powershell
uv run python -m analyse3.anchor.run_anchor_extraction --help
```

脚本优先读取命令行参数 `--api-key` 和 `--base-url`，其次读取 `.env` 中的 `LOCAL_OPENAI_API_KEY` 和 `LOCAL_OPENAI_BASE_URL`，再读取兼容的通用变量；当前默认模型为 `gpt-5.6-luna`。`LOCAL_OPENAI_BASE_URL` 可以写完整的 `http://localhost:8080/v1/chat/completions`，脚本会在交给 SDK 前自动转换为 API 根地址。也可以通过 `--env-file` 指定其他环境文件。

## 相似度连边评估

`evaluate_anchor_similarity.py` 使用本地 `sentence-transformers` 模型
`all-MiniLM-L6-v2` 将每个输出文件中的 `selected_anchor` 做 embedding，
再在 pair gold 上按方法分别扫描阈值。它会输出 Markdown 格式的每一对 cosine similarity、两套阈值表
（`all` 和排除 `relation=uncertain` 的 `definite`）、PR-AUC/AP、Precision、Recall、F1、
负例误连率和正例漏连率，并在默认 Precision >= 0.95 的 operating point 下落盘 FP/FN
及其 gold topic descriptor、relation、粒度和 null 标注。

例如，对当前 pilot 中的三个候选选择版本进行比较：

```powershell
uv run streamem-anchor-eval `
  --method v2=artifacts/anchor_dataset_v1/pilot_v1/anchor_outputs_candidate_selection_v2_standalone.jsonl `
  --method v3=artifacts/anchor_dataset_v1/pilot_v1/anchor_outputs_candidate_selection_v3_standalone.jsonl `
  --method v4=artifacts/anchor_dataset_v1/pilot_v1/anchor_outputs_candidate_selection_v4_coffee_adjusted.jsonl `
  --pairs artifacts/anchor_dataset_v1/pilot_v1/pilot_pair_candidates.jsonl `
  --gold artifacts/anchor_dataset_v1/pilot_v1/pilot_pair_gold_labels.jsonl `
  --segment-gold artifacts/anchor_dataset_v1/segment_gold_labels.jsonl `
  --embedding-model all-MiniLM-L6-v2 `
  --precision-target 0.95 `
  --out-dir artifacts/anchor_dataset_v1/pilot_v1/anchor_similarity_eval_v1
```

先安装本地评估依赖：

```powershell
uv sync --extra anchor-eval
```

模型权重会由 `sentence-transformers` 在首次运行时下载并缓存；anchor embedding 也会
缓存在输出目录中，重复运行不会重复计算已经缓存的 anchor。人类查看结果时优先打开
`summary.md`、各方法目录下的 `thresholds_*.md` 和 `pair_scores.md`；对应的 JSONL
仍然保留给程序继续处理。关键词、直接锚点等真正不同
的方法也应整理成同样的 JSONL 格式后通过多个 `--method name=file` 传入。当前的 v2/v3/v4 是 prompt/输出版本，
不应在论文中称为三种不同的锚点方法。
