---
name: oura
description: "Oura 智能戒指 — 睡眠、活动、准备度、压力、心率、血氧"
type: tool
expose_as_tool: true
requires:
  env: [OURA_CLIENT_ID, OURA_CLIENT_SECRET, OURA_REFRESH_TOKEN]
sense:
  interval: 30
writes:
  diary: [journal, today_ctx]
  db: [health_log]
config:
  diary_journal:
    type: bool
    default: true
    description: "Write sleep/workout events to journal"
  diary_today_ctx:
    type: bool
    default: true
    description: "Write sleep/cycle summary to today context"
---

## Tools

### get_oura_data (L0)
Query Oura Ring health data. Use for current or recent health questions (sleep, activity, readiness, stress, blood oxygen, heart rate, workouts). Returns structured data from Oura API cache.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| category | string | no | sleep / activity / readiness / stress / spo2 / heartrate / workout / all (default: all) |
| date | string | no | YYYY-MM-DD format. Default: today (falls back to yesterday if unavailable) |
| force_refresh | boolean | no | Set true to bypass the 10-min API cache and fetch fresh data |

## Usage Rules
- **Always call this tool** when the user asks about health, sleep, stress, activity, or status — even if you already have Oura data in conversation context. Never infer "no data" from prior results; always fetch fresh.
- **Stale data handling:** If the response has `"stale": true`, it means today's data hasn't synced yet and the result is yesterday's. Tell the user exactly that — do NOT present it as today's data. Check the `_note` field for details.
- **Force refresh:** If the user asks to re-check or get the latest data, pass `force_refresh: true` to bypass the API cache.

## Category Guide

| Category | When to use |
|----------|-------------|
| `sleep` | Sleep duration / quality / deep sleep / HRV |
| `activity` | Steps / calories / sedentary time / activity level |
| `readiness` | Readiness score / temperature / why readiness is low |
| `stress` | Stress level / recovery |
| `spo2` | Blood oxygen / breathing |
| `heartrate` | Heart rate / daytime heart rate |
| `workout` | Exercise / workouts / running |
| `all` | General health query / status overview |

## Response Gotchas
- `stress_high_hr` / `recovery_high_hr`: unit is **hours** (duration), NOT ratio
- `total_sleep_sec`: unit is **seconds** — divide by 3600 for hours
- `score` (sleep/activity/readiness): 0–100 score, NOT hours
- Oura records sleep under the date you **wake up**, not the date you fell asleep
