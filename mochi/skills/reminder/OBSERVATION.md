---
name: reminder
interval: 5
type: context
enabled: true
requires_config: []
skill_name: reminder
---

Surfaces unfired reminders due within the next 2 hours so heartbeat can trigger timely nudges.

## Fields
| Field | Type | Description |
|-------|------|-------------|
| upcoming | list[dict] | Reminders due soon: `[{message, remind_at}, ...]` |
