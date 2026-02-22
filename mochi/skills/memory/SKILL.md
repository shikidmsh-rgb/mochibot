---
name: memory
description: "长期记忆 — 保存、搜索、管理用户相关信息"
type: tool
expose_as_tool: true
core: true
---

## Tools

### save_memory (L1)
Save a piece of information about the user for future reference.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| content | string | yes | The information to remember |
| category | string | no | Category: preference, fact, event, habit, goal, emotion, general |

### recall_memory (L0)
Search through saved memories about the user.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | no | Search keywords to filter memories |
| category | string | no | Filter by category |

### update_core_memory (L1)
Add or delete a single entry in core memory. Use sparingly — for important, lasting facts only.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, delete) | yes | What to do |
| content | string | yes | add: the fact to append. delete: keyword to match which line to remove. |

### list_memories (L0)
List all saved memories, optionally filtered by category.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| category | string | no | Filter by category |
| limit | integer | no | Max items to return (default 30) |

### delete_memory (L1)
Delete a specific memory by ID (moved to trash, recoverable for 30 days).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| memory_id | integer | yes | The ID of the memory to delete |

### memory_stats (L0)
Show memory system statistics (total count, categories, trash size).

No parameters required.

### view_core_memory (L0)
Display the full core memory content.

No parameters required.

### memory_trash_bin (L0)
View or restore deleted memories from the trash bin.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, restore) | no | list (default) or restore |
| trash_id | integer | no | Required when action is restore — the trash item ID to restore |

## Usage Rules
- Use `save_memory` for facts, preferences, and events the user mentions — do NOT ask "should I save this?"
- Use `recall_memory` before answering questions about the user's history or preferences
- `update_core_memory` is for important, lasting facts only (name, role, key relationships) — not ephemeral details
- `delete_memory` soft-deletes to trash — user can restore within 30 days via `memory_trash_bin`
