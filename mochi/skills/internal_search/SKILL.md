---
name: internal_search
description: 在自己的聊天记录、Diary 和 Memory 中按关键词查找过去的信息
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

## Capability Context

- 搜索只读取 MochiBot 本地保存的资料，不会发起外部请求或修改记忆内容；Memory 的引用次数只在结果真正展示给 Main 后记录。
- all 会分别返回聊天、Diary 和 Memory 片段；每类最多 10 条，每条正文最多 280 个字符。
- 聊天和 Diary 使用文字匹配，Memory 使用已有的本地文字召回能力；聊天结果不包含当前这一轮的用户消息。
- 显式搜索覆盖仍保留的历史记录，包括 /reset 之前的记录；/reset 不删除资料。
- Diary 只搜索“今日日記”正文，不搜索状态区或明天草稿；归档扫描有数量和读取体积边界，达到边界会说明。
