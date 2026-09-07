# Graph experiment summary

## Input

```json
{
  "anchor_output": "F:\\StreamEM\\analyse3\\anchor\\artifacts\\anchor_dataset_v1\\output\\new_outputv2_full_manual.jsonl",
  "anchor_field": "selected_anchor",
  "input_row_count": 277,
  "node_count": 44,
  "available_session_count": 158,
  "selected_session_ids": [
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    24
  ],
  "selected_session_count": 24,
  "pair_count": 42,
  "non_empty_anchor_count": 44,
  "empty_anchor_count": 0,
  "definite_pair_count": 42,
  "embedding_model": "all-MiniLM-L6-v2",
  "embedding_stats": {
    "unique_texts": 44,
    "cache_misses": 44
  }
}
```

## Runs

| run_name | graph_rule | algorithm | resolution | edge_count | community_count | community_precision | community_recall | community_f1 | edge_precision | edge_recall | isolated_node_fraction | largest_community_fraction |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| knn_k10_tau0p50__leiden__r1p0 | knn_k10_tau0p50 | leiden | 1.000000 | 12 | 35 | 0.571429 | 1.000000 | 0.727273 | 0.571429 | 1.000000 | 0.636364 | 0.090909 |
| knn_k10_tau0p50__louvain__r1p0 | knn_k10_tau0p50 | louvain | 1.000000 | 12 | 35 | 0.571429 | 1.000000 | 0.727273 | 0.571429 | 1.000000 | 0.636364 | 0.090909 |
| knn_k5_tau0p50__leiden__r1p0 | knn_k5_tau0p50 | leiden | 1.000000 | 12 | 35 | 0.571429 | 1.000000 | 0.727273 | 0.571429 | 1.000000 | 0.636364 | 0.090909 |
| knn_k5_tau0p50__louvain__r1p0 | knn_k5_tau0p50 | louvain | 1.000000 | 12 | 35 | 0.571429 | 1.000000 | 0.727273 | 0.571429 | 1.000000 | 0.636364 | 0.090909 |
| mutual_knn_k10_tau0p50__leiden__r1p0 | mutual_knn_k10_tau0p50 | leiden | 1.000000 | 12 | 35 | 0.571429 | 1.000000 | 0.727273 | 0.571429 | 1.000000 | 0.636364 | 0.090909 |
| mutual_knn_k10_tau0p50__louvain__r1p0 | mutual_knn_k10_tau0p50 | louvain | 1.000000 | 12 | 35 | 0.571429 | 1.000000 | 0.727273 | 0.571429 | 1.000000 | 0.636364 | 0.090909 |
| mutual_knn_k5_tau0p50__leiden__r1p0 | mutual_knn_k5_tau0p50 | leiden | 1.000000 | 12 | 35 | 0.571429 | 1.000000 | 0.727273 | 0.571429 | 1.000000 | 0.636364 | 0.090909 |
| mutual_knn_k5_tau0p50__louvain__r1p0 | mutual_knn_k5_tau0p50 | louvain | 1.000000 | 12 | 35 | 0.571429 | 1.000000 | 0.727273 | 0.571429 | 1.000000 | 0.636364 | 0.090909 |
| threshold_0p50__leiden__r1p0 | threshold_0p50 | leiden | 1.000000 | 12 | 35 | 0.571429 | 1.000000 | 0.727273 | 0.571429 | 1.000000 | 0.636364 | 0.090909 |
| threshold_0p50__louvain__r1p0 | threshold_0p50 | louvain | 1.000000 | 12 | 35 | 0.571429 | 1.000000 | 0.727273 | 0.571429 | 1.000000 | 0.636364 | 0.090909 |
| knn_k5__louvain__r1p0 | knn_k5 | louvain | 1.000000 | 118 | 6 | 0.307692 | 1.000000 | 0.470588 | 0.307692 | 1.000000 | 0.000000 | 0.250000 |
| knn_k5__leiden__r1p0 | knn_k5 | leiden | 1.000000 | 118 | 6 | 0.285714 | 1.000000 | 0.444444 | 0.307692 | 1.000000 | 0.000000 | 0.272727 |
| knn_k10__louvain__r1p0 | knn_k10 | louvain | 1.000000 | 224 | 4 | 0.250000 | 1.000000 | 0.400000 | 0.266667 | 1.000000 | 0.000000 | 0.295455 |
| mutual_knn_k5__leiden__r1p0 | mutual_knn_k5 | leiden | 1.000000 | 71 | 10 | 0.272727 | 0.750000 | 0.400000 | 0.363636 | 1.000000 | 0.045455 | 0.272727 |
| mutual_knn_k5__louvain__r1p0 | mutual_knn_k5 | louvain | 1.000000 | 71 | 10 | 0.272727 | 0.750000 | 0.400000 | 0.363636 | 1.000000 | 0.045455 | 0.272727 |
| mutual_knn_k10__leiden__r1p0 | mutual_knn_k10 | leiden | 1.000000 | 169 | 5 | 0.235294 | 1.000000 | 0.380952 | 0.266667 | 1.000000 | 0.000000 | 0.272727 |
| mutual_knn_k10__louvain__r1p0 | mutual_knn_k10 | louvain | 1.000000 | 169 | 5 | 0.235294 | 1.000000 | 0.380952 | 0.266667 | 1.000000 | 0.000000 | 0.295455 |
| knn_k10__leiden__r1p0 | knn_k10 | leiden | 1.000000 | 224 | 4 | 0.222222 | 1.000000 | 0.363636 | 0.266667 | 1.000000 | 0.000000 | 0.386364 |
