---
name: reminder
expose: true
triggers: [tool_call]
---

## Tool: manage_reminder

Description: Create, list, or delete reminders for the user.

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| action | string | yes | One of: create, list, delete |
| message | string | no | Reminder message (required for create) |
| remind_at | string | no | ISO 8601 datetime for the reminder (required for create) |
| reminder_id | integer | no | Reminder ID (required for delete) |
