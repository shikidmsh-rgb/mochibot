---
name: memory
description: "长期记忆 — 保存、搜索、管理用户相关信息"
type: tool
expose_as_tool: true
core: true
always_on: true
---

## Tools

### save_memory (L1)
值得让你记住，但不需要每轮都知道的事情。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| content | string | yes | 要记住的信息 |
| category | string | no | 分类：偏好、事实、事件、习惯、目标、情绪、其他 |

### update_core_memory (L1)
关系性质、强偏好/禁忌、当前长期身份——你每轮都需要知道、不知道会说错话的事。听到这类信息或更正时调（例：刚确立关系、用户讨厌某称呼、刚换了职业身份）。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, delete) | yes | 操作类型 |
| content | string | yes | add：要添加的内容。delete：用于匹配要删除行的关键词。 |

### recall_memory (L0, extended)
搜索已保存的用户记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | no | 搜索关键词 |
| category | string | no | 按分类筛选 |

### list_memories (L0, extended)
列出已保存的记忆，可按分类筛选。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| category | string | no | 按分类筛选 |
| limit | integer | no | 最大返回条数（默认 30） |

### delete_memory (L1, extended)
按 ID 删除一条记忆（移入回收站，30 天内可恢复）。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| memory_id | integer | yes | 要删除的记忆 ID |

### memory_stats (L0, extended)
显示记忆系统统计（总数、分类分布、回收站大小）。

无需参数。

### view_core_memory (L0, extended)
显示完整的核心记忆内容。

无需参数。

### memory_trash_bin (L0, extended)
查看或恢复回收站中已删除的记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, restore) | no | list（默认）或 restore |
| trash_id | integer | no | restore 时必填——要恢复的回收站条目 ID |

## Usage Rules
- **save_memory / update_core_memory 每轮都可用**：聊天中遇到值得记的事，立即写入，不要拖到晚上自动整理
- `update_core_memory` 只存长期稳定的信息（名字、关系、情感联结、关键偏好），临时信息不要写进 core memory
- 管理类操作（recall / list / delete / stats / view_core / trash）按需通过 `request_tools(skills=["memory"])` 申请
