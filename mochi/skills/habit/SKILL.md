---
name: habit
description: "Habit tracking — create, check in, query, and manage recurring habits"
type: tool
tier: lite
expose_as_tool: true
observer: true
config:
  snooze_default_min:
    type: int
    default: 60
    description: "Default snooze duration in minutes"
  checkin_dedup_seconds:
    type: int
    default: 5
    description: "Guard window (seconds) for duplicate checkin detection"
---

# Habit Skill

Recurring habit tracking with flexible frequency, check-in, pause/snooze, and streak stats.

## Tools

### query_habit (L0)
Query habit list or statistics.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, stats) | yes | list = current progress; stats = 7-day/4-week history |
| habit_id | integer | no | Filter stats to a single habit |

### checkin_habit (L1)
Check in, undo, or snooze a habit.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: checkin, undo_checkin, snooze) | yes | What to do |
| habit_id | integer | yes | Habit ID |
| note | string | no | Optional note for the check-in |
| count | integer | no | Number of check-ins to record at once (default 1) |
| delay_minutes | integer | no | Snooze duration in minutes (default from config) |

### edit_habit (L1)
Add, remove, pause, resume, or update a habit.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, remove, pause, resume, update) | yes | What to do |
| name | string | no | Habit name (required for add; optional for update) |
| frequency | string | no | "daily:N", "weekly:N", or "weekly_on:DAY,...:N" (required for add; optional for update) |
| category | string | no | Category tag (e.g. health, pet, study) |
| importance | string (enum: important, normal) | no | Important habits get priority nudges (default normal) |
| context | string | no | Descriptive context (e.g. "morning and evening, after meals") |
| habit_id | integer | no | Required for remove/pause/resume/update |
| until | string | no | Pause end date YYYY-MM-DD (default: 7 days from now) |

## Usage Rules

- **Auto-checkin**: when user says they did something that matches a habit, call `checkin_habit` directly — don't ask for confirmation
- **Undo**: if user says "I didn't actually do X" or corrects themselves, call `undo_checkin`
- **Snooze**: when user says "later" / "not now" about a habit nudge, snooze it (creates a reminder at snooze-end)
- **Context field**: store timing hints like "morning and evening" or "after meals, 22:00" — the system uses these for smart nudge scheduling
- **Importance**: mark health/medication habits as "important" — they get priority tracking
- **No future checkins**: only check in for the current period (today for daily, this week for weekly)

## Frequency Examples

| User says | frequency value |
|-----------|----------------|
| "twice a day" | `daily:2` |
| "once a day" | `daily:1` |
| "3 times a week" | `weekly:3` |
| "weekends only" | `weekly_on:sat,sun:1` |
| "Mon/Wed/Fri" | `weekly_on:mon,wed,fri:1` |
