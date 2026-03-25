# Observers Registry

> Canonical list of all MochiBot observers. Update this file when adding or removing an observer.

---

## Observer Index

| Name | Description | Status | Interval | Requires Config |
|------|-------------|--------|----------|-----------------|
| [activity_pattern](#activity_pattern) | Conversation pattern detection — behavioral anomalies | ✅ on | 60 min | — |
| [habit](#habit) | Habit tracking data from local SQLite | ✅ on | 60 min | — |
| [oura](#oura) | Oura Ring health data (sleep, activity, readiness, stress) | ✅ on | 30 min | yes* |
| [recent_conversation](#recent_conversation) | Last ~10 conversation rounds for context | ✅ on | 20 min | — |
| [time_context](#time_context) | Temporal awareness (date, time-of-day, holidays, silence) | ✅ on | 20 min | — |
| [weather](#weather) | Current weather via OpenWeatherMap | ✅ on | 30 min | yes |

**Status legend**: ✅ on = enabled by default. Observers with missing `requires_config` env vars are auto-disabled at startup. To manually disable, set `enabled: false` in `OBSERVATION.md` or rename it to `OBSERVATION.md.disabled`.

*\* Oura observer self-checks config via `is_configured()` at runtime (returns `{}` when unconfigured) rather than using the framework `requires_config` field.*

---

## Details

### activity_pattern

| Field | Value |
|-------|-------|
| Path | `mochi/observers/activity_pattern/` |
| Interval | 60 min |
| Requires config | — |

Detects behavioral anomalies from SQLite message history — zero LLM calls. Signals include: `silent_after_active_day`, `unusually_quiet`, `below_average_activity`, `silent_N_days`, `high_engagement_today`.

### habit

| Field | Value |
|-------|-------|
| Path | `mochi/observers/habit/` |
| Interval | 60 min |
| Requires config | — |

Reads habit tracking data from local SQLite. Reports: active habits, logged today, due today, streaks, and summary.

### oura

| Field | Value |
|-------|-------|
| Path | `mochi/observers/oura/` |
| Interval | 30 min |
| Requires config | `OURA_CLIENT_ID`, `OURA_CLIENT_SECRET`, `OURA_REFRESH_TOKEN` (self-checked, not via framework `requires_config`) |

Fetches Oura Ring health data with 10-minute API cache. Covers sleep, activity, readiness, and stress scores. Auth via OAuth2 (`python oura_auth.py`).

### recent_conversation

| Field | Value |
|-------|-------|
| Path | `mochi/observers/recent_conversation/` |
| Interval | 20 min |
| Requires config | — |

Provides the last ~10 conversation rounds (20 messages) from SQLite. Fields: `messages`, `count`, `last_user_message`, `last_user_message_when`.

### time_context

| Field | Value |
|-------|-------|
| Path | `mochi/observers/time_context/` |
| Interval | 20 min |
| Requires config | — |

Pure-code temporal awareness — no external API. Fields: `date`, `weekday`, `hour`, `minute`, `time_of_day`, `is_weekend`, `is_holiday`, `silence_minutes`, `silence_hours`.

### weather

| Field | Value |
|-------|-------|
| Path | `mochi/observers/weather/` |
| Interval | 30 min |
| Requires config | `OPENWEATHER_API_KEY`, `WEATHER_LAT`, `WEATHER_LON` |

Current weather data via OpenWeatherMap free tier. Fields: `temperature_c`, `feels_like_c`, `condition`, `description`, `humidity`, `wind_kph`, `summary`.
