---
name: habit
description: "打卡 / check in — recurring daily or weekly habits"
type: tool
tier: lite
multi_turn: true
expose_as_tool: false
writes:
  diary: [diary]
  db: [habit_checkins]
config:
  checkin_dedup_seconds:
    type: int
    default: 5
    description: "Ignore duplicate single check-ins for the same habit within this window (race condition guard)"
---

# Habit Skill

Track recurring habits (e.g. feed cat medicine, study vocab, exercise).
User checks in via chat; diary status panel reflects progress in real time.

## Tools

### query_habit (L0)
Check habit progress and stats.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, stats) | yes | list = today's progress; stats = history |
| habit_id | integer | no | Habit ID (stats only) |

### checkin_habit (L1)
Record habit completion or undo.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: checkin, undo_checkin) | yes | What to do |
| habit_id | integer | yes | Habit ID |
| note | string | no | Optional note |
| count | integer | no | Checkins to record (default 1) |

### edit_habit (L1)
Create, remove, pause, resume, or update a habit.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, remove, pause, resume, update) | yes | What to do |
| habit_id | integer | no | Habit ID (remove/pause/resume/update) |
| name | string | no | Habit name (add required; update optional) |
| frequency | string | no | daily:N, weekly:N, or weekly_on:DAY,...:N (add required; update optional) |
| category | string | no | Category tag (e.g. health, pet, study) |
| importance | string | no | important or normal (add, default normal) |
| context | string | no | Schedule note (add) |
| until | string | no | ISO date for pause. Default: 7 days |

## Behavior Rules

- **Auto-checkin without asking**: When user reports completing a habit, immediately call `checkin_habit`. Don't ask for confirmation.
- **Current message only**: Only process habits mentioned in the current message.
- **NEVER checkin for future intent**: Only checkin for things ALREADY DONE, not plans.
- **Auto-undo on correction**: If user corrects a misunderstood checkin, call `undo_checkin`.
- **Delay**: When user says "晚点做", use `manage_reminder` to set a one-time reminder. Don't use `edit_habit(action=pause)` — pause is for multi-day breaks, not short delays.
