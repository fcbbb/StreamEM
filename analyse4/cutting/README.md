# analyse4 / cutting：可迭代的对话切割实验

这个目录把实验拆成四层：

```text
data/
  conversations/       原始 session，只读
  gold/initial_60.jsonl 已审核真值，只读
prompts/
  v001_fact_single/    一个可复现的 Prompt 版本
runs/
  <prompt>/<run>/      每次运行的 predictions、manifest、evaluation、人工 review
reports/               跨 run 的比较表
cutting_pipeline/      稳定的运行、解析、指标代码
```

## 核心约定

- `data/conversations` 是输入，不在运行过程中改写。
- `data/gold/initial_60.jsonl` 是当前开发集真值；它只用于评估，不会被模型运行改写。
- Prompt 不再写死在 Python 中。每个 `prompts/<version>/` 目录独立保存 `system.txt`、`user_template.txt` 和 `metadata.json`。
- 一个 run 只对应一次 Prompt + 模型 + 参数 + 数据入口。`run_manifest.json` 保存 Prompt 全文和 SHA-256，因此 Prompt 文件后续被编辑也不影响追溯。
- 预测结果、评估结果和段内纯度抽检都放在同一个 run 目录中。

## 第一次运行

在仓库根目录执行：

```powershell
uv run streamem-cutting-v4 run `
  --prompt-version v001_fact_single `
  --run-id baseline `
  --dry-run
```

确认无误后去掉 `--dry-run`。默认使用前 60 个有标注 session：

```powershell
uv run streamem-cutting-v4 run `
  --prompt-version v001_fact_single `
  --run-id baseline
```

结果会写入 `runs/v001_fact_single/baseline/`：

- `predictions.jsonl`：每个 session 一行，包含输入语义单元、预测区间、原始模型输出和状态；
- `run_manifest.json`：Prompt、哈希、模型、参数、数据路径和成功/失败统计；
- `evaluation.json` / `evaluation.md`：运行评价；
- `purity_review.jsonl`：人工段内纯度抽检模板（按需生成）。

## 迭代 Prompt

复制一个版本，修改新目录中的文件：

```powershell
Copy-Item prompts/v001_fact_single prompts/v002_strict_fact -Recurse
# 编辑 prompts/v002_strict_fact/system.txt 或 user_template.txt
uv run streamem-cutting-v4 run `
  --prompt-version v002_strict_fact `
  --run-id trial_001
```

建议每次只改变 Prompt，不要复用旧 run 目录。若需要继续中断的 run，使用同一个 run id 并加 `--resume`；若需要重跑已有 session，显式加 `--force`。

## 评价和人工纯度

```powershell
uv run streamem-cutting-v4 evaluate `
  --predictions runs/v001_fact_single/baseline/predictions.jsonl
```

抽取稳定的人工抽检模板：

```powershell
uv run streamem-cutting-v4 purity-template `
  --predictions runs/v001_fact_single/baseline/predictions.jsonl `
  --output runs/v001_fact_single/baseline/purity_review.jsonl `
  --sample-size 100
```

填写每行的 `pure` 为 `true` / `false` 后重新评价：

```powershell
uv run streamem-cutting-v4 evaluate `
  --predictions runs/v001_fact_single/baseline/predictions.jsonl `
  --purity runs/v001_fact_single/baseline/purity_review.jsonl
```

## 横向比较 Prompt

先分别完成各个 run 的 `evaluation.json`，再执行：

```powershell
uv run streamem-cutting-v4 compare `
  --runs runs/v001_fact_single/baseline runs/v002_strict_fact/trial_001 `
  --output reports/prompt_comparison.json
```

同时生成 JSON 和 Markdown 表，重点查看精确 Boundary F1、F1@±1、Pk、WindowDiff、漏切和过切；纯度填写后也会进入比较表。

## 数据和标注版本

当前迁入：158 个原始 session，以及 analyse3 中已审核的连续前 60 个 session 标注。若重新生成真值，运行：

```powershell
python analyse4/cutting/build_initial_annotations.py
```

这会只重写 `data/gold/initial_60.jsonl` 和对应索引，不会触碰原始对话或任何 run 产物。正式实验前建议复制出新的 gold 文件名，并在命令行通过 `--annotations` 显式指定。
