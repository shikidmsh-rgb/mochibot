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
| category | string | no | Category: preference, fact, event, habit, goal, emotion, general |

## Tool: recall_memory

Description: Search through saved memories about the user.

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| query | string | no | Search keywords to filter memories |
| category | string | no | Filter by category |

## Tool: update_core_memory

Description: Add or delete a single entry in core memory. Use sparingly — for important, lasting facts only.

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| action | string | yes | `add` or `delete` |
| content | string | yes | add: the fact to append. delete: keyword to match which line to remove. |

## Tool: list_memories

Description: List all saved memories, optionally filtered by category.

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| category | string | no | Filter by category |
| limit | integer | no | Max items to return (default 30) |

## Tool: delete_memory

Description: Delete a specific memory by ID (moved to trash, recoverable for 30 days).

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| memory_id | integer | yes | The ID of the memory to delete |

## Tool: memory_stats

Description: Show memory system statistics (total count, categories, trash size).

### Parameters
None.

## Tool: view_core_memory

Description: Display the full core memory content.

### Parameters
None.

## Tool: memory_trash_bin

Description: View or restore deleted memories from the trash bin.

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| action | string | no | `list` (default) or `restore` |
| trash_id | integer | no | Required when action is `restore` — the trash item ID to restore |
