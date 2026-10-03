---
name: internal_search
description: 查找自己的聊天记录、日记、记忆和工具结果。
type: tool
---

## Tools

### search_personal_history (on_demand, adaptive)
在本地保存的聊天记录、Diary 和 Memory 中搜索关键词或短语。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | yes | 要查找的关键词或短语，最多 200 个字符 |
| source | string (enum: all, conversation, diary, memory) | no | 搜索范围，默认 all |
| limit | integer | no | 每类最多返回条数，默认 5，范围 1-10 |

### read_tool_result (on_demand)
翻看一张回执对应的详细结果，不会重新执行原操作。长内容可以接着读；当时没有完整保留下来的内容会注明。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| receipt_id | integer | yes | 要查看的回执编号。 |
| offset | integer | no | 从上次返回的 next_offset 继续读；首次可省略。 |

## Capability Context

- 搜索只读取 MochiBot 本地保存的资料，不会发起外部请求或修改记忆内容。
- all 会分别返回聊天、Diary 和 Memory 片段。
- 聊天和 Diary 使用文字匹配，Memory 使用已有的本地文字召回能力；聊天结果不包含当前这一轮的用户消息。
- 显式搜索覆盖仍保留的历史记录，包括 /reset 之前的记录；/reset 不删除资料。
- Diary 只搜索“今日日記”正文，不搜索状态区或明天草稿。
