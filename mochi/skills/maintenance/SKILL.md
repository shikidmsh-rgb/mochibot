---
name: maintenance
description: "夜间记忆维护 — 去重、压缩、归档"
expose_as_tool: false
type: automation
triggers: [cron]
core: true
---

## Triggers
- type: cron
  schedule: 0 3 * * *
