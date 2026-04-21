---
name: note
description: "Heartbeat 工作便签 — agent 写给下次心跳的自己看的待办、条件型交代、临时关注"
type: tool
tier: lite
expose_as_tool: true
always_on: true
core: true
---

# Note Skill

agent 的便签条。每次 heartbeat 自动读到，用来跨周期保留"下次心跳要记得做什么"。

不适合：精确时间提醒（用 reminder）、定期打卡（用 habit）、关于用户的长期事实（用 memory）。

## Tools

### manage_note (L0)
下次 heartbeat 跳动时需要记得做的具体事情时调用。如："等他下班帮我问 X"、"压力高时多关心"。事情过去就 remove。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, list, remove, rewrite) | yes | add / list / remove / rewrite |
| content | string | | Note text. Required for add. |
| note_id | integer | | Required for remove. Line number from list output. |
| notes | array | | Required for rewrite. Complete replacement list — old notes are discarded. |

## Usage Rules

- 判断标准：**下次 heartbeat 醒来时，自己需要记得这件事吗？** 需要就记
