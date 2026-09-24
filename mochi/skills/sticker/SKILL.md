---
name: sticker
description: "贴纸 — 发送语境贴纸"
type: tool
exclude_transports: [wechat]
---

## Tools

### send_sticker (resident)
根据情绪或语义标签从贴纸库中选择并发送一张匹配贴纸，为当前表达增加非文字情绪。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| mood | string | yes | 情绪或语义标签，用中文：酷、自信、得意、生气、愤怒、不爽、崩溃、头晕、伤心、大哭、委屈、难过、困倦、疲惫、想睡觉。无精确匹配时随机发送。 |

### delete_last_sticker (on_demand)
删除本聊天最近发送的一张贴纸，结果说明是否真的找到并删除。

无需参数。

## Capability Context

- 聊天通道每条回复最多承载一张贴纸。
- 工具返回的 `[STICKER:...]` 是 transport 消费的发送协议，不是要展示给用户的文字。
