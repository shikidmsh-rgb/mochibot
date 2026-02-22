---
name: habit
interval: 60
enabled: true
requires_config: []
---

Provides habit tracking data from the local SQLite database.
No external API required — reads habits and logs created by the habit skill.

## Fields
| Field | Type | Description |
|-------|------|-------------|
| active_habits | number | Total number of active habits being tracked |
| logged_today | number | Habits already logged today |
| due_today | list | Habit names not yet logged today |
| streaks | list | Top habits by streak: [{name, streak_days}] |
| summary | string | Human-readable summary, e.g. "2/3 habits done today, 5-day meditation streak" |

## Notes
- Data is read-only here. Use the habit skill to create habits and log completions.
- Streak = consecutive days logged, counting back from today (or yesterday if today is not yet logged).
