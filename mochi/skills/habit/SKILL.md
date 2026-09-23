---
name: habit
description: 追踪每天或每周反复完成的目标与次数；同一请求还要求定时提醒时也需要 habit
type: tool
multi_turn: true
diary_status_order: 10
---

# Habit Skill

追踪需要长期坚持的习惯（如运动、喝水、学习）。用户通过聊天打卡，日记状态面板实时反映进度。

## Tools

### habit_progress (routed)
读取习惯进度或记录已经完成的进展；“打算做”或“晚点做”不算完成。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, stats, add, sync, undo) | yes | list = 当前进度；stats = 历史统计；add = 新增完成次数；sync = 对账到累计进度；undo = 撤销当前周期最近一次完成记录 |
| habit_id | integer | no | 习惯 ID；stats/add/sync/undo 使用 |
| habit_name | string | no | 唯一、精确的现有习惯名称；stats/add/sync/undo 时可代替 habit_id；同时提供时须与 ID 对应 |
| note | string | no | add/sync 的备注 |
| count | integer | no | 仅 add 使用；本次新增的完成次数，正整数，默认 1 |
| total | integer | no | 仅 sync 使用且必填；当前周期累计完成次数，非负整数；工具原子读取已存进度并只补齐差额，不减少已有记录 |

### edit_habit (routed)
创建或调整需要反复追踪的长期习惯，包括频率、重要性、暂停和恢复。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, remove, pause, resume, update) | yes | 操作类型 |
| habit_id | integer | no | 习惯 ID（remove/pause/resume/update 与 habit_name 二选一） |
| habit_name | string | no | 唯一、精确的现有习惯名称（remove/pause/resume/update 可替代 habit_id；同时提供时须与 ID 对应） |
| name | string | no | 习惯名称（add 必填；update 可选） |
| cycle | string (enum: daily, weekly) | no | 统计周期（add 必填；update 可选） |
| target | integer | no | 每个周期的目标次数，正整数；add 默认 1，update 省略表示保持不变 |
| weekdays | array | no | 仅 weekly 使用；可选 mon、tue、wed、thu、fri、sat、sun；add 省略表示不限星期，update 省略保留原设置，空数组清除星期限制 |
| category | string | no | 分类标签（如 health、pet、study） |
| importance | string (enum: important, normal) | no | important 或 normal（默认 normal） |
| context | string | no | 时间安排备注 |
| until | string | no | 暂停截止日期（YYYY-MM-DD），默认 7 天 |

## Capability Context

- add/sync 会改变完成计数，因此“打算做”或“晚点做”并不构成已完成事实。
- add 记录新增次数；sync 对账到当前周期累计值，重复提交相同累计值不会重复计数，低于已存进度时不会删除记录。实际完成次数可以超过目标。
- list 返回所有活跃习惯的当前进度；stats 不指定习惯时返回所有活跃习惯的历史统计。
- `pause` 会在多天范围内暂停追踪；它不会保存一次性的“稍后再看”念头。
- `remove` 停用习惯但保留历史；重新 add 同名的已停用习惯会恢复原 ID 和历史，并应用本次参数。
- update 只改变明确提供的字段；切换 cycle 时会清除不再适用的星期限制。
- 每个写操作只改变参数中明确指定的习惯，结果会返回实际状态。
