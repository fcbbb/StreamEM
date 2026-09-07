# 数据层

`conversations/` 保存原始 session，来源为 `analyse3/cutting/data/conversations/`，本实验将其视为只读输入。

`gold/initial_60.jsonl` 保存当前 60 个开发 session 的人工审核边界。每行包含：

- `units`：稳定的语义单元；
- `boundaries`：`after_unit_id` 到 `before_unit_id` 的真值边界；
- `segments`：由真值边界展开的连续区间；
- `annotation_status` / `annotator`：标注来源信息。

预测文件会保留一份实际使用的 `units`，评估时会再次和原始对话校验，防止输入切句变化后仍然误用旧标注。
