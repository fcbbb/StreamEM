# Stream Memory Graph TODO

更新时间：2026-09-07

## 待办一：社区纯化与 Boundary 处理

- [ ] 分析现有 `boundary`：区分真实主题交叉、anchor 过粗、memory 过宽、embedding 误连和证据不足。
- [ ] 接入社区纯化：放在固定 memory 拆分和 Boundary 标记之后、memory extraction/fusion 之前。
- [ ] 纯化必须保证每个 `segment_id` 恰好出现一次，不生成或删除 segment，并记录原始输入、输出、分组映射和错误。
- [ ] 暂不自动归属、删除或强制融合 Boundary；纯化失败时保守回退，不能把混合社区直接写入 memory。
- [ ] 对比无纯化、纯化但不处理 Boundary、纯化并进行明确 Boundary 后处理三种方案，评估社区纯度、Boundary、memory 数量和最终检索/回答效果。

纯化接入顺序：

```text
活动图
→ 固定 memory 拆分 / Boundary 标记
→ 社区纯化
→ memory extraction / fusion
→ 归档 segment
```

## 待办二：关键词/实体增强的高召回建图

- [ ] 为每个 segment 和 memory 维护关键词集合、实体集合；memory 同时保留聚合集合及历史成员集合。
- [ ] 建图时合并语义、关键词和实体候选边；实体或稀有关键词可形成弱边，统一计算边权后交给 Leiden。
- [ ] 检查阈值和 top-k 逻辑，避免词法/实体候选在高召回建图阶段被过滤掉。
- [ ] 对比仅语义建图、加入关键词、加入实体、两者都加入四种方案，重点评估同主题召回率和跨主题误连率。
