---
name: activity_pattern
interval: 60
enabled: true
requires_config: []
---

Conversation pattern detection from SQLite message history.
Zero LLM calls. No external API. Detects behavioral changes that hint at
user state — useful for the heartbeat Think step to decide if a check-in
is warranted.

## Fields
| Field | Type | Description |
|-------|------|-------------|
| today_messages | number | User messages sent today |
| yesterday_messages | number | User messages sent yesterday |
| daily_avg_7d | number | Average messages/day over past 7 days (active days only) |
| active_days_7d | number | Days in past 7 with 3+ messages |
| weekly_trend | list | Per-day counts [{date, count}] for last 7 days |
| signals | list | Detected anomalies (see below) — absent if no signals |

## Signals

| Signal | Meaning |
|--------|---------|
| `silent_after_active_day` | Yesterday had messages, today is silent |
| `unusually_quiet` | Today count < 30% of personal average |
| `below_average_activity` | Slightly below average but not zero |
| `silent_N_days` | N consecutive days with zero messages |
| `high_engagement_today` | Today is >2x personal average |

## Notes
- interval=60: patterns don't change minute-to-minute, hourly check is enough
- Signals are absent when behavior is normal (don't flood LLM with noise)
- The Think prompt can use signals to decide if a proactive message is warranted
