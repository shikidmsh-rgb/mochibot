---
name: oura
expose: true
triggers: [tool_call]
---

## Tool: get_oura_data

Description: Query Oura Ring health data. Use for current or recent health questions (sleep quality, activity, readiness, stress). Returns structured data from Oura API cache. For historical trends (>7 days), use recall_memory instead.

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| category | string | no | sleep / activity / readiness / stress / all (default: all) |
| date | string | no | YYYY-MM-DD format. Default: today (falls back to yesterday if unavailable) |
