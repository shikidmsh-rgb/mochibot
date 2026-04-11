---
name: diary
description: "日記後台 — diary status auto-refresh (automation)"
type: automation
expose_as_tool: false
---

# Diary (Automation)

Backend automation for the unified diary (`data/diary.md`). No LLM-exposed tools — this is **not a skill in the tool sense**. The handler is a stub that exists only so diary remains a toggleable unit in the skill registry.

Refreshes the **今日状態** section from DB (habits, todos, reminders) on heartbeat tick.

## Infrastructure

- `mochi/diary.py`: `DailyFile` class — L4 infrastructure for diary file I/O (append, upsert, remove, rewrite, archive).
- `mochi/diary.py`: `refresh_diary_status()` — rebuilds 今日状態 from DB, called by heartbeat tick and habit checkin.
