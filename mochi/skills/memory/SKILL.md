---
name: memory
expose: true
triggers: [tool_call]
---

## Tool: save_memory

Description: Save a piece of information about the user for future reference.

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| content | string | yes | The information to remember |
| category | string | no | Category: preference, fact, event, habit, goal, general |

## Tool: recall_memory

Description: Search through saved memories about the user.

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| query | string | no | Search keywords to filter memories |
| category | string | no | Filter by category |

## Tool: update_core_memory

Description: Directly update the core memory summary (use sparingly, for important corrections only).

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| content | string | yes | The updated core memory content |
