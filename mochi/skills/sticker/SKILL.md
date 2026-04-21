---
name: sticker
description: "贴纸 — 发送语境贴纸"
type: tool
expose_as_tool: true
always_on: true
tier: lite
exclude_transports: [wechat]
---

## Tools

### send_sticker (L0)
根据情绪或语义标签从贴纸库中发送匹配的贴纸。当贴纸能增强回复的情感表达时调用。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| mood | string | yes | 情绪或语义标签，用中文：酷、自信、得意、生气、愤怒、不爽、崩溃、头晕、伤心、大哭、委屈、难过、困倦、疲惫、想睡觉。无精确匹配时随机发送。 |

### delete_last_sticker (L0, extended)
删除最近发送的贴纸。用户表示不喜欢刚发的贴纸时调用（如"这个表情包不好看，删掉"、"删掉这个贴纸"、"这个表情不好"）。

无需参数。

## Usage Rules
- 每条回复最多 1 张贴纸
- 工具返回特殊标记，回复中不要描述贴纸内容
- mood 参数用中文标签
