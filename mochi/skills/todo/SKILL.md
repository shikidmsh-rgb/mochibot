---
name: todo
description: "待办 — add, list, complete, delete one-off tasks"
type: tool
expose_as_tool: true
writes:
  db: [todos]
---

# Todo Skill

## Usage Rules

- **todo = 无固定时间的待办**（"买猫粮"、"约牙医"、"查一下XXX"）
- **reminder = 有明确时间的提醒**（"3点开会"、"每天吃药"）→ use `manage_reminder` instead
- 用户说 "帮我记一下要XXX" / "别忘了XXX" → **todo**
- 用户说 "提醒我XXX" / "N点叫我" → **reminder**
- `nudge_date`: set when user implies a soft deadline ("这周内搞定", "月底前") — bot will remind on that date
- All pending todos (due today, overdue, or no date) appear in diary 今日状態 with `[todo_id=X]`. Use that ID to complete/delete. No need to call list first.

## Tools

### manage_todo (L1)
Manage todos: add, list, complete, delete, or update.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string | yes | add / list / complete / delete / update |
| task | string | | Task description (action=add, or new text for action=update). |
| todo_id | integer | | Todo ID (action=complete/delete/update). |
| nudge_date | string | | Date (YYYY-MM-DD) for soft reminder via heartbeat (action=add/update). When set, bot will proactively remind the user on that date. |
| include_done | boolean | | Include completed items (action=list). Default false. |
