# 锚点设计 v2：面向记忆对象的 Targeted Anchor

> 当前实际提取契约以 `anchor_extraction_prompt_v8_target_aspect.md` 为准：模型只
> 输出 `core_target` 和 `aspect` 两个字段。本文中的其他结构化字段是设计
> 推演和内部分析，不属于当前模型输出格式。

## 结论

锚点要服务的不是“这段话的摘要相似度”，而是判断两个片段是否应该
进入同一个可持续更新的记忆对象。因此，聚类对象定义为：

> **稳定目标（stable target）**：一个可以被多段对话继续补充、修改或引用的
> 对象、命题、实践或事件。

“片段主要讲了什么”仍然有用，但它只能作为 facet，不能单独作为聚类键。

## 两种语义视图

每个片段输出两个视图：

| 字段 | 作用 | 是否进入聚类相似度 |
| --- | --- | --- |
| `cluster_anchor` | 稳定目标名（必要时加类型前缀）；跨片段尽量保持不变 | 是 |
| `detail_anchor` | 稳定目标 + 本段具体方面/操作 | 否，作为展示和记忆生成依据 |

同时保存结构化字段，便于调试和后续规则：

- `target_type`：由模型按片段内容生成的简短角色标签，例如项目、文档、
  实践、概念或事件；它是开放世界字段，不是预先穷举的类别表；
- `target_name`：不包含本段操作和局部方面的规范名称；
- `facet`：本段讨论的局部方面；
- `operation`：`add`、`update`、`delete`、`discuss`、`compare`、`plan` 或 `other`。

`selected_anchor` 继续保留，以便旧的提取器和查看工具不崩溃；在 targeted
v2 中它应等于 `detail_anchor`，而不是作为图聚类字段。

## 为什么能解决当前两个反例

### 时间管理社区 41

期望的抽取方向如下：

| 片段 | `target_type` | `target_name` | `facet` |
| --- | --- | --- | --- |
| `session_0033_seg002` | `personal_practice` | `personal time-management practice` | busyness, meaningful productivity and task breakdown |
| `session_0033_seg003` | `personal_practice` | `personal time-management practice` | task prioritization and time-management techniques |
| `session_0033_seg004` | `ai_capability` | `human-like time management by AI` | motivation, well-being and individual preferences |
| `session_0036_seg002` | `social_policy` | `shorter workweeks and societal work-time allocation` | productivity, stress and economic resistance |
| `session_0046_seg002` | `personal_practice` | `personal time-management practice` | planning versus spontaneity |

这三个 target 名称不能都退化成 `time management`。目标类型是语义的一部分，
不是可有可无的分类标签，但它不构成封闭分类体系。目标名本身必须保留足够
的范围信息；类型前缀是否进入 `cluster_anchor` 由后续稳定性实验决定，例如：

```text
personal time-management practice
human-like time management by AI
shorter workweeks and societal work-time allocation
```

这样个人实践、AI 能力和社会政策之间不会因为共享一个词而形成直接边。

### 项目提案及后续修改

对于新增、更新、删除同一项目，操作和 facet 可以不同，但 `target_name` 必须
从对话中恢复同一个项目标题，并且不能把 `update`、预算、stakeholder 等放进
`cluster_anchor`：

```text
project: deep learning for regional energy demand forecasting under climate volatility
```

具体的预算修改、交付物更新等只进入 `facet` 和 `detail_anchor`。因此它们可以
归属于同一个项目 memory item，同时仍然保留各自的细节。

## 聚类规则

1. 图节点使用 `cluster_anchor`，不再使用 `selected_anchor`。
2. `detail_anchor` 不参与第一阶段连边；它用于展示、摘要和第二阶段的细粒度
   去重/冲突检测。
3. `target_type` 只是辅助特征，不是完整本体，也不应默认作为硬门控。只有
   在独立验证证明类型标签稳定后，才可以把它用于限制直接边。
4. 同一 `project` / `document` 的不同 facet 允许进入同一个 memory item。
5. 没有稳定 target 的片段可以保留 `null`，不能用泛化词强行建立边。

## 评测需要同步改变

现有 `same_specific_topic` 仍可作为严格主题评测，但端到端图还应增加：

- `same_memory_item`：是否属于同一个可更新对象；
- `same_target_different_facet`：target 相同、facet 不同；
- `different_target_same_family`：同一 broad family 但不能聚类。

否则“同项目不同方面应该连接”和“同一大类不同对象不应连接”会被混成一个
指标，无法指导锚点设计。

## 防止类别预设和数据泄露

正式评测前冻结 Prompt、schema 和聚类规则。Prompt 中只能使用与数据集无关
的合成示例，不得出现当前数据集的 session、项目标题、gold topic、人工
标签或从测试片段改写出的例子。

类型字段采用开放世界字符串，并允许 `null`；覆盖性不由预先枚举保证，而由
开发集上的“稳定目标召回率”和测试集上的错误归并率检验。测试集标签只能在
Prompt 和规则冻结后生成或解封。

至少保留两种测试切分：

1. **unseen-target split**：测试中的目标名称和目标组合不出现在开发示例中，
   检验模型能否发现新对象；
2. **repeated-target split**：同一目标的不同 facet/操作分散到测试中，检验
   项目更新、文档修改等是否能够重新对齐。

实验清单应记录 Prompt 的 hash、冻结时间、开发/测试 session 列表和生成
结果时间；任何在看过测试结果后改 Prompt 的版本只能算新的实验，不能覆盖
原测试结论。

本 v2 方案是在检查当前社区和 pair 结果之后提出的，因此它是一个**开发假设**，
不能把当前已经检查过的 277 个片段及其 pair 直接当作无偏测试集。当前数据应
标为 development/adaptation set；要报告泛化指标，必须另外封存一批在方案设计
时没有查看过的 session，或采集一批新的对话。即使 Prompt 中没有复制测试文本，
根据测试错误模式反向调参也属于评测适应，不能声称对该测试集完全无泄露。

这里还要区分两件事：Prompt/实验流程的“直接数据泄露”可以通过版本、hash 和
内容审计来检查；模型预训练阶段是否见过外部同源语料，则不能仅凭本实验文件
证明，需要依赖模型来源和训练数据声明。
