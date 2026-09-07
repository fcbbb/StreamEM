# Graph experiment summary

## Input

```json
{
  "anchor_output": "F:\\StreamEM\\analyse3\\anchor\\artifacts\\anchor_dataset_v1\\output\\new_outputv2_full_manual.jsonl",
  "anchor_field": "selected_anchor",
  "input_row_count": 277,
  "node_count": 89,
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
    24,
    25,
    26,
    27,
    28,
    29,
    30,
    31,
    32,
    33,
    34,
    35,
    36,
    37,
    38,
    39,
    40,
    41,
    42,
    43,
    44,
    45,
    46,
    47,
    48
  ],
  "selected_session_count": 48,
  "pair_count": 178,
  "non_empty_anchor_count": 89,
  "empty_anchor_count": 0,
  "definite_pair_count": 178,
  "embedding_model": "all-MiniLM-L6-v2",
  "embedding_stats": {
    "unique_texts": 89,
    "cache_misses": 89
  }
}
```

## Runs

| run_name | graph_rule | algorithm | resolution | edge_count | community_count | community_precision | community_recall | community_f1 | edge_precision | edge_recall | isolated_node_fraction | largest_community_fraction |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| knn_k10_tau0p50__leiden__r1p0 | knn_k10_tau0p50 | leiden | 1.000000 | 52 | 53 | 0.678571 | 0.678571 | 0.678571 | 0.703704 | 0.678571 | 0.370787 | 0.067416 |
| knn_k10_tau0p50__louvain__r1p0 | knn_k10_tau0p50 | louvain | 1.000000 | 52 | 53 | 0.678571 | 0.678571 | 0.678571 | 0.703704 | 0.678571 | 0.370787 | 0.067416 |
| knn_k5_tau0p50__leiden__r1p0 | knn_k5_tau0p50 | leiden | 1.000000 | 52 | 53 | 0.678571 | 0.678571 | 0.678571 | 0.703704 | 0.678571 | 0.370787 | 0.067416 |
| knn_k5_tau0p50__louvain__r1p0 | knn_k5_tau0p50 | louvain | 1.000000 | 52 | 53 | 0.678571 | 0.678571 | 0.678571 | 0.703704 | 0.678571 | 0.370787 | 0.067416 |
| mutual_knn_k10_tau0p50__leiden__r1p0 | mutual_knn_k10_tau0p50 | leiden | 1.000000 | 52 | 53 | 0.678571 | 0.678571 | 0.678571 | 0.703704 | 0.678571 | 0.370787 | 0.067416 |
| mutual_knn_k10_tau0p50__louvain__r1p0 | mutual_knn_k10_tau0p50 | louvain | 1.000000 | 52 | 53 | 0.678571 | 0.678571 | 0.678571 | 0.703704 | 0.678571 | 0.370787 | 0.067416 |
| mutual_knn_k5_tau0p50__leiden__r1p0 | mutual_knn_k5_tau0p50 | leiden | 1.000000 | 52 | 53 | 0.678571 | 0.678571 | 0.678571 | 0.703704 | 0.678571 | 0.370787 | 0.067416 |
| mutual_knn_k5_tau0p50__louvain__r1p0 | mutual_knn_k5_tau0p50 | louvain | 1.000000 | 52 | 53 | 0.678571 | 0.678571 | 0.678571 | 0.703704 | 0.678571 | 0.370787 | 0.067416 |
| threshold_0p50__leiden__r1p0 | threshold_0p50 | leiden | 1.000000 | 52 | 53 | 0.678571 | 0.678571 | 0.678571 | 0.703704 | 0.678571 | 0.370787 | 0.067416 |
| threshold_0p50__louvain__r1p0 | threshold_0p50 | louvain | 1.000000 | 52 | 53 | 0.678571 | 0.678571 | 0.678571 | 0.703704 | 0.678571 | 0.370787 | 0.067416 |
| mutual_knn_k5__louvain__r1p0 | mutual_knn_k5 | louvain | 1.000000 | 136 | 14 | 0.396226 | 0.750000 | 0.518519 | 0.487805 | 0.714286 | 0.044944 | 0.157303 |
| mutual_knn_k5__leiden__r1p0 | mutual_knn_k5 | leiden | 1.000000 | 136 | 15 | 0.400000 | 0.714286 | 0.512821 | 0.487805 | 0.714286 | 0.044944 | 0.157303 |
| mutual_knn_k10__louvain__r1p0 | mutual_knn_k10 | louvain | 1.000000 | 281 | 9 | 0.333333 | 0.892857 | 0.485437 | 0.383333 | 0.821429 | 0.011236 | 0.235955 |
| mutual_knn_k10__leiden__r1p0 | mutual_knn_k10 | leiden | 1.000000 | 281 | 9 | 0.328947 | 0.892857 | 0.480769 | 0.383333 | 0.821429 | 0.011236 | 0.235955 |
| knn_k10__leiden__r1p0 | knn_k10 | leiden | 1.000000 | 490 | 6 | 0.313953 | 0.964286 | 0.473684 | 0.362319 | 0.892857 | 0.000000 | 0.258427 |
| knn_k10__louvain__r1p0 | knn_k10 | louvain | 1.000000 | 490 | 6 | 0.313953 | 0.964286 | 0.473684 | 0.362319 | 0.892857 | 0.000000 | 0.247191 |
| knn_k5__louvain__r1p0 | knn_k5 | louvain | 1.000000 | 233 | 7 | 0.256098 | 0.750000 | 0.381818 | 0.425532 | 0.714286 | 0.000000 | 0.269663 |
| knn_k5__leiden__r1p0 | knn_k5 | leiden | 1.000000 | 233 | 7 | 0.253012 | 0.750000 | 0.378378 | 0.425532 | 0.714286 | 0.000000 | 0.269663 |
