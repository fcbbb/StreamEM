# 对话事件切割实验

`cutting_experiment.py` 是兼容启动入口，实际实现位于 `cutting_pipeline/`：

- `prompt.py`：英文 System/User Prompt、JSON 解析和分段校验；
- `data.py`：session、JSONL 和语义单元处理；
- `llm.py`：OpenAI-compatible API 单次调用；
- `metrics.py`：边界指标、Pk、WindowDiff 和错误分布；
- `purity.py`：段内纯度人工抽检模板；
- `workflow.py` / `cli.py`：实验运行、评价和命令行入口。

实际发送给模型的 Prompt 为英文，版本号为 `conversation-cutting-en-v2-gold-aligned`。Prompt 明确要求将开头 greeting、实质 interaction、结尾 goodbye 对齐到当前 gold 标注规则。默认模型为 `gpt-5.6-luna`，通过 OpenCode Go 的 `/responses` 接口调用。Luna 不接受 `temperature` 参数，因此 Responses 请求会省略该参数；Chat Completions 模型仍使用配置的 temperature。认证、额度和模型不支持类错误不会自动重试；如需改用 Chat Completions 模型，可显式传入 `--model glm-5.3-flash`。

默认使用 `annotations/initial_60.jsonl` 的 60 个 session 作为评估集。标注文件中的 `initial_for_review` 状态会被原样保留；脚本不会改写人工标注。

## 运行切割

先做输入和语义单元检查：

```bash
uv run streamem-cutting run --dry-run
```

使用固定 Prompt 和默认模型运行：

```bash
uv run streamem-cutting run \
  --output-dir artifacts/cutting_gpt4o_mini
```

API 配置默认从仓库根目录的 `.env` 加载，切割专用配置优先使用 `CUTTING_OPENAI_API_KEY` 和 `CUTTING_OPENAI_BASE_URL`，然后回退到 `GRAPH_WEEKLY_OPENAI_*`、`OPENAI_*` / `OPENROUTER_API_KEY`。命令行的 `--api-key` 和 `--base-url` 优先级最高，也可以通过 `--env-file` 指定其他环境文件。HTTP 代理默认开启，会读取 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`；如需关闭请添加 `--no-proxy`。

```bash
uv run streamem-cutting run \
  --session-ids 1,2 \
  --output-dir artifacts/cutting_smoke
```

断点续跑：

```bash
uv run streamem-cutting run \
  --output-dir artifacts/cutting_gpt4o_mini \
  --resume
```

如要对全部 158 个原始 session 运行（没有 gold 的 session 不能评价），加 `--session-source all`。

每个 session 完成后会写入 `predictions.jsonl`，运行配置和 Prompt 写入 `run_manifest.json`。模型输出会被严格校验：必须覆盖全部语义单元，且区间连续；格式错误会记录为失败 session，不会静默转成空分段。

## 评价

```bash
uv run streamem-cutting evaluate \
  --predictions artifacts/cutting_gpt4o_mini/predictions.jsonl
```

输出 `evaluation.json` 和同名 Markdown 报告。报告包含：

- 精确 Boundary Precision / Recall / F1；
- 一对一匹配的 Boundary F1@±1（辅助指标，精确 F1 仍是主要指标）；
- Pk、WindowDiff；
- 漏切数与过切数；
- `session_type`、`operation` 以及标注中可选 `scenario_types` 的分层统计。

±1 容差需要两名标注者的分歧数据来决定是否纳入正式结论。当前标注文件只有一层标注，因此报告会明确标记它是辅助结果。

## 段内纯度人工抽检

先生成稳定的抽检模板：

```bash
uv run streamem-cutting purity-template \
  --predictions artifacts/cutting_gpt4o_mini/predictions.jsonl \
  --output artifacts/cutting_gpt4o_mini/purity_review.jsonl \
  --sample-size 100
```

人工将每行的 `pure` 填为 `true` 或 `false`，再把文件传给评价器：

```bash
uv run streamem-cutting evaluate \
  --predictions artifacts/cutting_gpt4o_mini/predictions.jsonl \
  --purity artifacts/cutting_gpt4o_mini/purity_review.jsonl
```

`pure` 的定义是：一个预测事件是否没有包含两个可以独立形成记忆锚点的交互目标。未填写的样本不会进入纯度分母。
