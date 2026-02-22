---
name: todo
expose: true
triggers: [tool_call]
---

## Tool: manage_todo

Description: Create, list, complete, or delete todo items.

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| action | string | yes | One of: add, list, complete, delete |
| task | string | no | Task description (required for add) |
| category | string | no | Optional category tag |
| todo_id | integer | no | Todo ID (required for complete/delete) |
