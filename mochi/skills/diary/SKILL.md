---
name: diary
description: Daily working memory — fast-changing context for the current day
expose_as_tool: true
type: tool
multi_turn: false
---

## Tools

### read_diary
Read today's diary entries. Returns the current day's working memory.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | no | Optional keyword to filter entries |

### update_diary
Add or rewrite diary entries for today.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string | yes | "append" to add an entry, "rewrite" to replace all entries |
| content | string | yes | The diary entry text |

## Usage Rules
- Use `update_diary` to note important things that happened today (meals, events, mood, decisions)
- Diary resets daily — it is working memory, not permanent storage
- For permanent facts, use `save_memory` instead
- Keep entries concise — one line per observation
