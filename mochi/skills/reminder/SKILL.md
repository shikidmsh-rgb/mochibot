---
name: reminder
description: 管理明确时间的触发；只负责到点联系，不记录每天或每周目标的完成次数
type: tool
multi_turn: true
diary_status_order: 30
sense:
  interval: 5
---

## Tools

### schedule_self_reminder (resident)
给未来的自己留下一个明确时间的回望意图；到时结合新的事实、相处上下文和可用能力重新判断。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| intent | string | yes | 未来重新判断的方向，不是预写给用户的话 |
| remind_at | string | yes | ISO 8601 格式的首次回望时间 |
| recurrence | string (enum: one_time, daily, weekdays, weekly) | no | 默认 one_time；daily 每天，weekdays 周一至周五，weekly 每周 |

### manage_reminder (routed)
管理定时联系：notify 到点直接通知用户；self 到点让未来的自己结合当时情况重新判断。提醒可一次性或周期重复，但不追踪事情后来是否完成。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: create, list, update, delete) | yes | 操作类型 |
| kind | string (enum: notify, self) | no | 仅 create 使用；默认 notify，update 不改变类型 |
| message | string | no | notify 的完整提醒内容；create 必填，update 可修改；到点发送时仅添加前缀 ⏰ 和一个空格，不另行改写 |
| intent | string | no | self 的私有回望方向，不是预写给用户的话；create 必填，update 可修改 |
| remind_at | string | no | ISO 8601 格式；create 必填，update 可修改 |
| recurrence | string (enum: one_time, daily, weekdays, weekly) | no | create 省略表示 one_time；update 省略表示保持不变，传 one_time 可取消周期 |
| reminder_id | integer | no | list 返回的提醒 ID（update/delete 必填） |

## Capability Context

- schedule_self_reminder 直接创建 self，不需要先加载 manage_reminder。
- `notify` 到点直接发送已存 message，不会再调用模型改写。
- 每次触发只在预定时间后的 5 分钟内有效，过期作废、不补发；已过期的时间不会创建或更新成功。
- remind_at 是首次触发时间，之后按 recurrence 周期继续，错过的次数不连续补发。
- reminder 只理解时间，不理解“遛狗后”一类事件条件，也不会追踪完成状态；长期目标与进度属于 habit，一次性事项属于 todo，时间安排由 reminder 保存。
- `self` 保存的是 Main 留给未来自己的私有意图。到点时未来 Main 会结合当时的 Core、Diary、近期相处和可用能力重新判断；它可能行动、开口或安静结束。周期 self 无论这次是否开口都会安排下一次触发。
