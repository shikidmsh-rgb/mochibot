---
name: todo
interval: 20
type: context
enabled: true
requires_config: []
skill_name: todo
---

Exposes the number of active (not done) todos so heartbeat can mention pending tasks.

## Fields
| Field | Type | Description |
|-------|------|-------------|
| active_count | int | Number of pending todos |
