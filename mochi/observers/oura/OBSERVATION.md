---
name: oura
interval: 30
enabled: true
requires_config: []
---

Provides Oura Ring health data: sleep, activity, readiness, and stress.
Wraps oura_client.get_daily_summary() with a 10-min API cache.

## Fields
| Field | Type | Description |
|-------|------|-------------|
| sleep.total_hours | number | Total sleep duration |
| sleep.deep_hours | number | Deep sleep duration |
| sleep.rem_hours | number | REM sleep duration |
| sleep.efficiency | number | Sleep efficiency % |
| sleep.avg_hrv | number | Average HRV during sleep |
| sleep_score | number | Oura sleep score (0-100) |
| activity.steps | number | Steps today |
| activity.active_calories | number | Active calories burned |
| readiness.score | number | Readiness score (0-100) |
| stress.day_summary | string | Stress day summary |
| data_date | string | Date of this data (YYYY-MM-DD) |
| sleep_not_synced | bool | True if sleep data is from a previous day |

## Config

Oura uses OAuth2 for authentication. Run `python oura_auth.py` to authorize.

| Env Var | Required | Description |
|---------|----------|-------------|
| OURA_CLIENT_ID | yes | OAuth2 client ID from your Oura app |
| OURA_CLIENT_SECRET | yes | OAuth2 client secret |
| OURA_REFRESH_TOKEN | yes | Obtained by running `oura_auth.py` |
