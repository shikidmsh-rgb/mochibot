---
name: habit
description: "Habit progress — active habits with completion, streaks, and context"
interval: 60
enabled: true
requires_config: []
skill_name: habit
---

Provides habit tracking data from the local SQLite database.
No external API required — reads habits and logs created by the habit skill.

## Fields

| Field | Type | Description |
|-------|------|-------------|
| items | list | Active habit items (see below) |
| total_count | int | Total active (non-paused, non-snoozed) habits |
| incomplete_count | int | Habits with remaining checkins today |

### Habit Item Fields

| Field | Type | Description |
|-------|------|-------------|
| id | int | Habit ID |
| name | string | Habit name |
| cycle | string | "daily" or "weekly" |
| target | int | Required completions per cycle |
| done | int | Completions in current period |
| remaining | int | Target minus done (0 if complete) |
| importance | string | "important" or "normal" |
| category | string | Category tag |
| context | string | Descriptive context (e.g. "morning and evening") |
| active_today | bool | False for weekly_on habits on non-allowed days |
| last_checkin_at | string | ISO timestamp of last checkin, or null |
| streak | int | Consecutive completed periods (0 for important habits) |

## Notes

- Daily habits reset at MAINTENANCE_HOUR (not midnight)
- Weekly habits reset on Monday (ISO week)
- `weekly_on` habits are marked `active_today: false` on non-allowed days
- Paused and snoozed habits are excluded from observation
- Streak is only computed for non-important habits (important = task-type, not growth)
