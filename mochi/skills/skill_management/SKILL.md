---
name: skill_management
description: "统一设置 — 作息、时区、Free Time、技能开关与参数、个人开发、工具加载和模型信息"
type: tool
locked: true
---

# Settings

## Tools

### manage_settings (resident)
查看或修改你的设置。list 获取设置目录和当前值；get 查看一项的含义、限制和生效方式；set 修改一项；reset 按该项说明恢复。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, get, set, reset) | yes | 操作 |
| group | string (enum: runtime, skills, tools, models) | no | 仅 list 使用；省略列出全部类别 |
| id | string | no | get、set、reset 必填；使用目录中的完整设置 ID |
| value | string | no | 仅 set 必填；按设置类型传文本，例如 "23"、"8.5"、"true"、"Shanghai"、"routed"；空字符串是空值，不代表重置 |

## Capability Context

- 运行设置、技能开关和参数的修改依据用户的明确意图；工具加载方式可以由你自主调整。
- 技能关闭、缺少配置或尚未激活时，也可以在这里查看。新出现的技能工具可通过 request_tools 加载。
