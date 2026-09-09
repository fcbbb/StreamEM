# 提示词来源说明

本目录集中保存日流式记忆图实际运行时使用的 Prompt，其规则来自以下文件：

- `analyse3/cutting/cutting_pipeline/prompt.py`
- `analyse3/anchor/artifacts/anchor_dataset_v1/pilot_v1/anchor_extraction_prompt_v1.md`
- `analyse3/memory_extract/memory_extract_prompt_v6.md`
- `analyse3/memory_fusion/memory_fusion_prompt_v1.md`

集中后的文件移除了原设计文档中的实验说明、调用命令和中英文重复内容，仅保留运行所需的任务定义、保留规则和输出格式。

各模块会在调用时追加实际输入数据：

- `cutting.txt`：由切割模块追加 semantic units；
- `anchor_extraction.txt`：由锚点模块一次追加同一切割窗口产生的全部 segment；模型必须按 `segment_id` 原序返回一项一锚点，调用方会拒绝缺失、重复或虚构 ID；
- `memory_extraction.txt`：由记忆模块追加纯化后的单主题社区；
- `memory_fusion.txt`：由记忆模块追加完整的 existing memory 和新 segment group。
- `community_purification.txt`：由 checkpoint 阶段追加一个已按最高 memory 相似度分配好的 community group；输入包含至多一个已有 memory 节点和新 segment 节点，模型可以返回一个或多个 node group，并把不属于该 memory 的 segment 分到无 memory 的新主题 group。拆分得到的单 segment group 由调用方保留在 active graph，不再调用 extraction/fusion；拆分组之间的旧图边也会被切断。

Prompt 返回结果仍会由调用方执行严格 JSON 字段、稳定 ID 和来源可追溯性验证。

当前运行版 fusion Prompt 在原操作协议上增加了 `source_segment_ids`：模型只负责指出每项 `add/update/delete` 由本轮哪些 segment 直接支持，调用方会验证这些 ID 确实存在于 `new_group`。memory 节点级的完整 `source_segments/source_anchors` 仍由代码维护，不允许模型自由改写。

条目身份与证据身份严格分离：`add` 不输出 ID，调用方会生成独立稳定的 `item_id`；只有 `update/delete` 使用输入中已有的 `item_id`。`segment_id` 只允许出现在 `source_segment_ids` 中，不再充当记忆条目 ID。
