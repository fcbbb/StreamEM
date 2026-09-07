# 每日主题锚点记忆图

本包是 `stream_memory_graph_weekly.py` 的模块化后续版本。它负责将对话切割为连续事件、为每个事件提取一个语义锚点、构建单层活动图，并在每个完整日期结束后执行 checkpoint，最终把已经确认的社区压缩成稳定的结构化记忆节点。

社区检测使用 Leiden；运行前请安装图依赖：`uv sync --extra graph`。

第一次 checkpoint 之后，社区检测仅处理当天新增批次能够触达的图连通区域，其中包括被新节点连接到的 boundary 节点。未受影响的已有记忆不会被重复处理。

memory 在活动图中是简化超节点：以 `memory.topic` 为主表示，同时保留已压缩成员的历史 anchor。社区压缩后，原始 segment 会从活动图中移除，但其 anchor 仍参与超节点与新 segment 的聚合相似度计算，完整原文和来源关系也继续保存在审计状态中。

## 图约束

- 允许建立 `new segment → new segment` 边和 `new segment → memory` 边。
- `segment → memory` 边先取 topic 与历史成员 anchor 的最大相似度，再根据达到 `new_memory_threshold` 的成员覆盖率和平均支持强度增加共识分；大多数成员都相似时，超节点边会高于普通单条边。
- 禁止建立 `memory → memory` 边。
- 一个经过处理的社区最多只能包含一个 committed memory。
- 如果检测结果包含多个 memory，系统会根据固定种子的图支持度进行拆分。
- 无法明确归属的 segment 会被标记为 `boundary`，在后续 checkpoint 解决歧义之前不会进入记忆融合。
- memory 节点关系由一个默认关闭的预留存储接口表示；当前版本不会让这些关系参与活动图或社区检测。

## 命令行运行

输入可以是包含完整对话的 JSON/JSONL，也可以是已经切割完成的 segment JSONL。查看完整参数：

```powershell
python -m stream_memory_graph_daily --help
```

处理完整对话目录的示例：

```powershell
python -m stream_memory_graph_daily `
  --input analyse3/cutting/data/conversations `
  --input-type conversations `
  --state-out output/daily_memory_state.json `
  --llm-model gpt-4o-mini `
  --encoder-model all-MiniLM-L6-v2
```

生成的状态文件是一个自包含的 JSON 快照，可以通过 `--state-in` 恢复并继续处理。

如果使用 `--input-type segments` 传入预切割 JSONL，每行必须包含：

- `segment_id`
- `text`
- `date` 或 `event_date`
- `anchor` 或 `selected_anchor`

如果没有提供 anchor，系统会调用锚点提取 LLM。

使用已经带有 anchor 的 segment 时，可以增加 `--no-llm` 执行离线图与状态冒烟测试。需要记忆提取或融合的社区将保持 active，并出现在 `retry_segment_ids` 中；系统不会伪造降级记忆。

## Python 接口

```python
from stream_memory_graph_daily import DailyMemoryGraph, SegmentRecord

graph = DailyMemoryGraph(llm=my_json_llm, encoder=my_encoder)
graph.ingest_segment(
    SegmentRecord(
        segment_id="session_1_seg001",
        text="[user] I spent $3.66 on coffee.",
        anchor="coffee purchase",
        event_date="2025-06-01",
    )
)
graph.finalize()
graph.save("daily_memory_state.json")
```

`my_json_llm.complete(system_prompt, user_prompt)` 需要返回已经解析的 JSON 对象。包内的 `OpenAIJsonLLM` 提供了兼容 OpenAI 接口的默认实现。

## 评测

项目已经适配两阶段评测流程：

```powershell
# 阶段一：读取 158 个 session，构建并持久化记忆图
python stream_memory_graph_daily/evaluate/conversation_to_memory.py

# 阶段二：加载记忆状态，检索、回答 15 道问题并执行逐项评测
python stream_memory_graph_daily/evaluate/memory_to_answer.py
```

评测输入、输出文件和恢复方式详见 [evaluate/README.md](evaluate/README.md)。
