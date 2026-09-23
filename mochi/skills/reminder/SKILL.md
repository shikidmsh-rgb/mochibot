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

- `create` 持久化一个明确的 ISO 8601 时间，以及 notify 的通知内容或 self 的私有意图。schedule_self_reminder 直接创建 self，不需要先加载 manage_reminder。
- `notify` 到点发送已存 message，格式为 `⏰ {message}`，不会再调用模型改写；发送重试复用已准备的内容。
- `notify` 和 `self` 的每次触发都只在预定时间后的 5 分钟内有效；短暂失败可在窗口内重试，到期后作废，不再调用模型、发送或补发旧稿。创建和更新时间的回执会给出截止时间，已过期的时间不会创建或更新成功。
- recurrence 支持 one_time、daily、weekdays、weekly。remind_at 是首次触发时间，之后按所选周期继续；过期的次数保留证据，只安排下一次未过期的触发，不连续补发错过的日期。
- reminder 只理解时间，不理解“遛狗后”一类事件条件，也不会追踪完成状态；长期目标与进度属于 habit，一次性事项属于 todo，时间安排由 reminder 保存。
- `update` 仅修改尚未开始执行的提醒；已进入 Main、准备发送或重试中的本次提醒不能改写，以免丢失已执行动作或重发已送达内容。相同参数不会产生状态变化。
- `delete` 取消尚未送达且当前未被执行者占用的提醒；正在执行的提醒可能无法取消，结果会返回实际状态。
- `self` 保存的是 Main 留给未来自己的私有意图。到点时未来 Main 会结合当时的 Core、Diary、近期相处和可用能力重新判断；它可能行动、开口或安静结束。
- Self Reminder 的 Main 结果会先持久化再发送，窗口内的发送重试不会重复进入 Main 或重复执行工具；过期不撤销已经完成的工具操作。周期 self 在行动、开口或安静结束后都会安排下一次触发。
