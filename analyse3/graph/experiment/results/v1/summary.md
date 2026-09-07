# Graph experiment summary

## Input

```json
{
  "anchor_output": "F:\\StreamEM\\analyse3\\anchor\\artifacts\\anchor_dataset_v1\\output\\new_outputv2_full_manual.jsonl",
  "anchor_field": "selected_anchor",
  "input_row_count": 277,
  "node_count": 277,
  "pair_count": 1576,
  "non_empty_anchor_count": 277,
  "empty_anchor_count": 0,
  "definite_pair_count": 1505,
  "embedding_model": "all-MiniLM-L6-v2",
  "embedding_stats": {
    "unique_texts": 273,
    "cache_misses": 0
  }
}
```

## Runs

| run_name | graph_rule | algorithm | resolution | edge_count | community_count | community_precision | community_recall | community_f1 | edge_precision | edge_recall | isolated_node_fraction | largest_community_fraction |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mutual_knn_k5_tau0p50__leiden__r1p0 | mutual_knn_k5_tau0p50 | leiden | 1.000000 | 281 | 107 | 0.679070 | 0.651786 | 0.665148 | 0.812500 | 0.348214 | 0.227437 | 0.064982 |
| knn_k10_tau0p50__leiden__r1p0 | knn_k10_tau0p50 | leiden | 1.000000 | 461 | 98 | 0.611538 | 0.709821 | 0.657025 | 0.755435 | 0.620536 | 0.212996 | 0.086643 |
| mutual_knn_k10_tau0p50__leiden__r1p0 | mutual_knn_k10_tau0p50 | leiden | 1.000000 | 430 | 98 | 0.611538 | 0.709821 | 0.657025 | 0.788235 | 0.598214 | 0.212996 | 0.086643 |
| mutual_knn_k5_tau0p50__louvain__r1p0 | mutual_knn_k5_tau0p50 | louvain | 1.000000 | 281 | 106 | 0.665138 | 0.647321 | 0.656109 | 0.812500 | 0.348214 | 0.227437 | 0.064982 |
| mutual_knn_k10_tau0p50__louvain__r1p0 | mutual_knn_k10_tau0p50 | louvain | 1.000000 | 430 | 98 | 0.609195 | 0.709821 | 0.655670 | 0.788235 | 0.598214 | 0.212996 | 0.083032 |
| threshold_0p50__louvain__r1p0 | threshold_0p50 | louvain | 1.000000 | 488 | 98 | 0.614173 | 0.696429 | 0.652720 | 0.743590 | 0.647321 | 0.212996 | 0.115523 |
| knn_k5_tau0p50__louvain__r1p0 | knn_k5_tau0p50 | louvain | 1.000000 | 345 | 99 | 0.599237 | 0.700893 | 0.646091 | 0.777778 | 0.437500 | 0.212996 | 0.090253 |
| threshold_0p50__leiden__r1p0 | threshold_0p50 | leiden | 1.000000 | 488 | 98 | 0.606299 | 0.687500 | 0.644351 | 0.743590 | 0.647321 | 0.212996 | 0.119134 |
| knn_k5_tau0p50__leiden__r1p0 | knn_k5_tau0p50 | leiden | 1.000000 | 345 | 99 | 0.595420 | 0.696429 | 0.641975 | 0.777778 | 0.437500 | 0.212996 | 0.083032 |
| knn_k10_tau0p50__louvain__r1p0 | knn_k10_tau0p50 | louvain | 1.000000 | 461 | 98 | 0.591603 | 0.691964 | 0.637860 | 0.755435 | 0.620536 | 0.212996 | 0.122744 |
| mutual_knn_k5__louvain__r1p0 | mutual_knn_k5 | louvain | 1.000000 | 419 | 44 | 0.506849 | 0.660714 | 0.573643 | 0.675214 | 0.352679 | 0.064982 | 0.111913 |
| mutual_knn_k5__leiden__r1p0 | mutual_knn_k5 | leiden | 1.000000 | 419 | 43 | 0.495017 | 0.665179 | 0.567619 | 0.675214 | 0.352679 | 0.064982 | 0.101083 |
| mutual_knn_k10__louvain__r1p0 | mutual_knn_k10 | louvain | 1.000000 | 857 | 21 | 0.449857 | 0.700893 | 0.547993 | 0.615044 | 0.620536 | 0.021661 | 0.133574 |
| mutual_knn_k10__leiden__r1p0 | mutual_knn_k10 | leiden | 1.000000 | 857 | 21 | 0.437673 | 0.705357 | 0.540171 | 0.615044 | 0.620536 | 0.021661 | 0.104693 |
| knn_k10__leiden__r1p0 | knn_k10 | leiden | 1.000000 | 1369 | 12 | 0.336595 | 0.767857 | 0.468027 | 0.558052 | 0.665179 | 0.000000 | 0.166065 |
| knn_k5__leiden__r1p0 | knn_k5 | leiden | 1.000000 | 694 | 18 | 0.320236 | 0.727679 | 0.444748 | 0.628931 | 0.446429 | 0.014440 | 0.108303 |
| knn_k5__louvain__r1p0 | knn_k5 | louvain | 1.000000 | 694 | 18 | 0.320236 | 0.727679 | 0.444748 | 0.628931 | 0.446429 | 0.014440 | 0.108303 |
| knn_k10__louvain__r1p0 | knn_k10 | louvain | 1.000000 | 1369 | 9 | 0.293913 | 0.754464 | 0.423029 | 0.558052 | 0.665179 | 0.000000 | 0.281588 |
