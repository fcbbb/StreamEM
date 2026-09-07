# 图社区检测实验运行说明

## 目录

```text
experiment/
├── configs/       实验参数
├── src/           模块化代码
├── results/       运行结果，不放代码
└── RUN.md         本说明
```

## 输入

默认读取：

```text
F:\StreamEM\analyse3\anchor\artifacts\anchor_dataset_v1\output\new_outputv2_full_manual.jsonl
```

该文件应包含 277 条成功记录。默认配置从中选择前 48 个 session（共 89 个 segment）作为节点。按当前数据的日期字段，这一范围覆盖 `2025-06-01` 至 `2025-06-03`；session 数量不是每天固定 24 个，因此以 session 窗口为准。默认使用 `selected_anchor` 作为节点文本。空 anchor 记录仍保留为节点，但不产生语义边；社区检测结果中通常表现为单节点社区，涉及它们的 pair 仍参与端到端评估。

评估标签读取：

```text
anchor_dataset_v1\pair_candidates.jsonl
anchor_dataset_v1\pair_gold_labels.jsonl
```

标签只用于评估社区结果，不参与建图。

## 安装依赖

在仓库根目录 `F:\StreamEM` 执行：

```powershell
uv sync --extra graph
```

其中 `graph` extra 提供 sentence-transformers、igraph 和 leidenalg。

## 运行

```powershell
Set-Location F:\StreamEM\analyse3\graph\experiment
uv run python src\run_experiment.py --config configs\default.json
```

也可以指定其他输出目录：

```powershell
uv run python src\run_experiment.py `
  --config configs\default.json `
  --out-dir results\my_run
```

## 结果

默认输出到：

```text
results/two_days_v1/
├── summary.md                 汇总表
├── summary.json               机器可读汇总
├── input_summary.json         输入检查和 embedding 信息
├── run_config.json            本次配置
├── embedding_cache.json       本次运行生成的本地缓存
├── similarity_matrix.npy      89×89 相似度矩阵
└── runs/<run_name>/
    ├── metrics.json            单次运行指标
    ├── communities.jsonl       segment_id -> community_id
    ├── pair_predictions.jsonl  pair 的社区判断及 gold 标签
    └── edges.jsonl             实际生成的图边及权重
```

## 主要指标

主指标是社区级 pairwise 指标：

```text
pred_same = 两个 segment 的 community_id 是否相同
gold_same = pair_gold_labels 中的 same_specific_topic
```

排除 `relation=uncertain` 后计算 community precision、recall 和 F1。

同时输出直接边级指标和图结构指标，但它们是辅助指标。当前标签只覆盖候选 pair，因此结果应理解为候选 pair 上的评估。

## 注意

- 当前默认实验比较固定阈值、kNN、mutual-kNN 及其与 `similarity >= 0.5` 的组合；Leiden 是主算法，Louvain 是传统基线。
- 当前方案是静态 277 节点实验，不是流式更新实验。
- 如果改用 `fine_candidate`，需要在配置中将 `anchor_field` 改为 `fine_candidate`，并使用独立结果目录。
- 如果只想先检查代码链路，可以先把 `community_detection.algorithms` 改为 `["louvain"]`；正式结果仍应安装 Leiden 依赖并运行默认配置。
