---
name: reminder
interval: 5
type: context
enabled: true
requires_config: []
skill_name: reminder
---

Caches upcoming reminders due within the next 2 hours for Main to inspect through look_around. Reminder delivery remains owned by the reminder timer.

## Fields
| Field | Type | Description |
|-------|------|-------------|
| upcoming | list[dict] | Reminders due soon: `[{message, remind_at}, ...]` |
