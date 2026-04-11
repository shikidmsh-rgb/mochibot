---
name: todo
description: "Todo list — create, list, complete, and delete tasks"
type: tool
expose_as_tool: true
---

## Tools

### manage_todo (L1)
Create, list, complete, or delete todo items.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, list, complete, delete) | yes | What to do |
| task | string | no | Task description (required for add) |
| category | string | no | Optional category tag |
| todo_id | integer | no | Todo ID (required for complete/delete) |

## Usage Rules
- When user says "I need to X" or "add X to my list", create the todo directly
- `list` returns incomplete todos by default
- Mark todos `complete` when user says they finished something on their list
