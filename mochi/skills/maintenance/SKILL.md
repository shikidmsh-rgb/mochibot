---
name: maintenance
description: Nightly memory hygiene — dedup, compress, archive
expose_as_tool: false
type: automation
triggers: [cron]
---

## Triggers
- type: cron
  schedule: 0 3 * * *
