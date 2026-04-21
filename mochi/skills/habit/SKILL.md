---
name: habit
description: "习惯打卡 — 需要长期坚持并追踪'做了没有'的事（如运动、喝水、学习）。"
type: tool
tier: lite
multi_turn: true
diary_status_order: 10
expose_as_tool: false
writes:
  diary: [diary]
  db: [habit_checkins]
---

# Habit Skill

追踪需要长期坚持的习惯（如运动、喝水、学习）。用户通过聊天打卡，日记状态面板实时反映进度。

## Tools

### query_habit (L0)
用户问习惯进度或历史统计时调用。如："今天打卡了哪些"、"我这个月跑步几次了"。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, stats) | yes | list = 今日进度；stats = 历史统计 |
| habit_id | integer | no | 习惯 ID（仅 stats 需要） |

### checkin_habit (L1)
用户汇报做了一件需要长期坚持的事时调用。如："喝了今天第三杯水"、"刚跑完步"。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: checkin, undo_checkin) | yes | 操作类型 |
| habit_id | integer | yes | 习惯 ID |
| note | string | no | 备注 |
| count | integer | no | 打卡次数（默认 1） |

### edit_habit (L1)
用户想开始追踪一个长期习惯，或调整已有习惯时调用。如："以后帮我记每天喝水"、"暂停跑步打卡一周"。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, remove, pause, resume, update) | yes | 操作类型 |
| habit_id | integer | no | 习惯 ID（remove/pause/resume/update） |
| name | string | no | 习惯名称（add 必填；update 可选） |
| frequency | string | no | daily:N、weekly:N 或 weekly_on:DAY,...:N（add 必填；update 可选） |
| category | string | no | 分类标签（如 health、pet、study） |
| importance | string | no | important 或 normal（默认 normal） |
| context | string | no | 时间安排备注 |
| until | string | no | 暂停截止日期（ISO 格式），默认 7 天 |

## Behavior Rules

- 只处理当前消息提到的习惯
- 只为**已完成的事**打卡，用户说"打算做"不算
- **"晚点做"** → 用 `manage_note` 记下来。`pause` 是多天暂停用的
