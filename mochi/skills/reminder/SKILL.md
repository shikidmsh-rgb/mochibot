---
name: reminder
description: "定时提醒 — 到点通知一下，不追踪完成情况。"
type: tool
diary_status_order: 30
sense:
  interval: 5
---

## Tools

### manage_reminder (routed)
创建、查看或删除在指定时间主动送达的一次性通知。它记录“到点联系”，不追踪事情后来是否完成。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: create, list, delete) | yes | 操作类型 |
| kind | string (enum: notify, self) | no | create 时使用；默认 notify |
| message | string | no | notify 的提醒内容 |
| intent | string | no | self 的私有回望方向，不是预写给用户的话 |
| remind_at | string | no | ISO 8601 格式的提醒时间（create 必填） |
| reminder_id | integer | no | 提醒 ID（delete 必填） |

## Capability Context

- `create` 持久化提醒内容与一个明确的 ISO 8601 时间；到点后系统会用 Mochi 的身份发起一次已授权联系。
- `notify` 和 `self` 都只在预定时间后的 5 分钟内有效；短暂失败可在窗口内重试，到期后作废，不再调用模型、发送或补发旧稿。创建回执会给出截止时间，已过期的时间不会创建成功。
- reminder 只理解时间，不理解“遛狗后”一类事件条件，也不会追踪完成状态。持续关注的信息可由 Main 审慎写入 Core；持续追踪分别属于 habit 或 todo。
- `delete` 取消尚未送达的提醒，并返回被取消的 reminder ID。
- `self` 保存的是 Main 留给未来自己的私有意图。到点时未来 Main 会结合当时的 Core、Diary、近期相处和可用能力重新判断；它可能行动、开口或安静结束。
- Self Reminder 只接受一次性时间。其 Main 结果会先持久化再发送，窗口内的发送重试不会重复进入 Main 或重复执行工具；过期不撤销已经完成的工具操作。
