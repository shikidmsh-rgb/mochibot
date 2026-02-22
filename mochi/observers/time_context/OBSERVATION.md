---
name: time_context
interval: 20
enabled: true
requires_config: []
---

Pure-code time awareness — no external API, no config required.

Provides the heartbeat with structured temporal context so the LLM can
reason about "now" without guessing.

## Fields
| Field | Type | Description |
|-------|------|-------------|
| date | string | Current date (YYYY-MM-DD) |
| weekday | string | Day name (Monday, Tuesday, ...) |
| hour | number | Current hour (0-23) |
| minute | number | Current minute (0-59) |
| time_of_day | string | early_morning / morning / lunch / afternoon / evening / night / late_night |
| is_weekend | boolean | true if Saturday or Sunday |
| is_holiday | boolean | true if date matches known holiday list |
| holiday_name | string | Holiday name (only present if is_holiday=true) |
| silence_minutes | number | Minutes since last user message |
| silence_hours | number | Same as above, in hours (e.g. 2.5) |

## Extending Holidays

Edit the `_FIXED_HOLIDAYS` set in observer.py to add your country's holidays.
Future versions may support external holiday APIs.

## Notes
- interval=20 matches heartbeat frequency — time context is always fresh
- No rate limiting needed (pure local computation)
