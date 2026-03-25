# Skills Registry

> Canonical list of all MochiBot skills. Update this file when adding or removing a skill.

---

## Skill Index

| Name | Description | Status | Type |
|------|-------------|--------|------|
| [diary](#diary) | Daily working memory — fast-changing context for the current day | ✅ on | tool |
| [maintenance](#maintenance) | Nightly memory hygiene — dedup, outdated removal, salience rebalance, archive | ✅ on | automation |
| [memory](#memory) | Permanent memory storage and retrieval | ✅ on | tool |
| [oura](#oura) | Oura Ring health data access | ✅ on | tool |
| [reminder](#reminder) | Reminder management | ✅ on | tool |
| [sticker](#sticker) | Telegram sticker sending from learned registry | ✅ on | tool |
| [todo](#todo) | Todo / task list management | ✅ on | tool |

**Status legend**: ✅ on = enabled by default. To disable a skill, rename its `SKILL.md` → `SKILL.md.disabled`.

---

## Details

### diary

| Field | Value |
|-------|-------|
| Path | `mochi/skills/diary/` |
| Type | tool |
| Expose as tool | yes |
| Tools | `read_diary`, `update_diary` |
| Triggers | tool_call |

Daily working memory for the current day. Entries are auto-archived nightly by the maintenance skill.

### maintenance

| Field | Value |
|-------|-------|
| Path | `mochi/skills/maintenance/` |
| Type | automation |
| Expose as tool | no |
| Triggers | cron (`0 3 * * *`) |

Runs nightly at 3 AM: diary archive → memory dedup → outdated removal → salience rebalance → core audit → trash purge → summary.

### memory

| Field | Value |
|-------|-------|
| Path | `mochi/skills/memory/` |
| Type | tool |
| Expose as tool | yes |
| Tools | `save_memory`, `recall_memory`, `update_core_memory`, `list_memories`, `delete_memory`, `memory_stats`, `view_core_memory`, `memory_trash_bin` |
| Triggers | tool_call |

Three-layer persistent memory with 8 tools. Save/recall facts, add/delete core memory lines, browse and manage memories, soft-delete with 30-day trash recovery. Importance levels: ★1 routine, ★2 important, ★3 critical.

### oura

| Field | Value |
|-------|-------|
| Path | `mochi/skills/oura/` |
| Type | tool |
| Expose as tool | yes |
| Tools | `get_oura_data` |
| Triggers | tool_call |
| Requires config | `OURA_CLIENT_ID`, `OURA_CLIENT_SECRET`, `OURA_REFRESH_TOKEN` |

Query Oura Ring sleep, activity, readiness, and stress data.

### reminder

| Field | Value |
|-------|-------|
| Path | `mochi/skills/reminder/` |
| Type | tool |
| Expose as tool | yes |
| Tools | `manage_reminder` |
| Triggers | tool_call |

Create, list, and delete reminders with ISO 8601 datetime support.

### sticker

| Field | Value |
|-------|-------|
| Path | `mochi/skills/sticker/` |
| Type | tool |
| Expose as tool | yes |
| Tools | `send_sticker`, `delete_last_sticker` |
| Triggers | tool_call |

Send contextual stickers from a learned registry based on mood/semantic tags. Users teach stickers by forwarding them; the bot auto-generates Chinese tags via LLM. Supports delete of last sent sticker.

### todo

| Field | Value |
|-------|-------|
| Path | `mochi/skills/todo/` |
| Type | tool |
| Expose as tool | yes |
| Tools | `manage_todo` |
| Triggers | tool_call |

Add, list, complete, and delete tasks with optional categories.
