---
name: memory
description: "长期记忆 — 保存、搜索、管理用户相关信息"
type: tool
expose_as_tool: true
core: true
---

## Tools

### save_memory (L1)
保存一条关于用户的信息，供未来参考。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| content | string | yes | 要记住的信息 |
| category | string | no | 分类：偏好、事实、事件、习惯、目标、情绪、其他 |

### recall_memory (L0)
搜索已保存的用户记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | no | 搜索关键词 |
| category | string | no | 按分类筛选 |

### update_core_memory (L1)
新增或删除一条核心记忆。谨慎使用——仅限重要、长期稳定的信息。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, delete) | yes | 操作类型 |
| content | string | yes | add：要添加的内容。delete：用于匹配要删除行的关键词。 |

### list_memories (L0)
列出已保存的记忆，可按分类筛选。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| category | string | no | 按分类筛选 |
| limit | integer | no | 最大返回条数（默认 30） |

### delete_memory (L1)
按 ID 删除一条记忆（移入回收站，30 天内可恢复）。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| memory_id | integer | yes | 要删除的记忆 ID |

### memory_stats (L0)
显示记忆系统统计（总数、分类分布、回收站大小）。

无需参数。

### view_core_memory (L0)
显示完整的核心记忆内容。

无需参数。

### memory_trash_bin (L0)
查看或恢复回收站中已删除的记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, restore) | no | list（默认）或 restore |
| trash_id | integer | no | restore 时必填——要恢复的回收站条目 ID |

## Usage Rules
- `update_core_memory` 只存长期稳定的信息（名字、关系、情感联结、关键偏好），临时信息不要写进 core memory
