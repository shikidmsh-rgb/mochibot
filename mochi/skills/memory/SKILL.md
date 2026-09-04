---
name: memory
description: "长期记忆 — 更新常驻 Core，并搜索或管理后台整理的 Memory Items"
type: tool
locked: true
---

## Tools

### update_core (resident)
需要修改每轮常驻的自我、用户或关系摘要时调用。Core 是一份持续修订的自由文本文档，不是事件追加日志；标题仅为可选的可读性组织方式。Core 不能改变作息、时区或主动消息上限；这些运行设置使用 `manage_agent_settings`。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: edit, delete, insert_after, batch) | yes | Patch 操作；已有内容用 edit，真正独立的新内容才用 insert_after |
| content | string | no | insert_after：作为独立文本块插在唯一 anchor_text 后的内容 |
| old_text | string | no | edit/delete：必须与当前 Core 中一段原文精确且唯一匹配 |
| new_text | string | no | edit：替换后的原文 |
| anchor_text | string | no | insert_after：必须与当前 Core 中一段原文精确且唯一匹配的文本锚点，不是固定 section 或字段 |
| operations | array (items: object) | no | batch：同一份 Core 快照上按顺序原子执行 edit/delete/insert_after；每项使用对应参数，任一步失败则不写入也不创建快照 |

### recall_memory (on_demand)
搜索已保存的用户记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | no | 搜索关键词 |

### list_memories (on_demand)
列出已保存的记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| limit | integer | no | 最大返回条数（默认 30） |

### delete_memory (on_demand)
按 ID 删除一条记忆（移入回收站，30 天内可恢复）。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| memory_id | integer | yes | 要删除的记忆 ID |

### memory_stats (on_demand)
显示记忆系统统计（总数、重要记忆和回收站大小）。

无需参数。

### view_core_memory (on_demand)
显示完整的核心记忆内容。

无需参数。

### memory_trash_bin (on_demand)
查看或恢复回收站中已删除的记忆。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, restore) | no | list（默认）或 restore |
| trash_id | integer | no | restore 时必填——要恢复的回收站条目 ID |

## Usage Rules
- **update_core 每轮都可用**：Main 只维护每轮常驻的稳定 Core
- 具体 Memory Items 由后台 Lite 按聊天批次整理；Main 不直接创建条目
- Core 是持续修订的文档，不是事件流水账；新事实优先合并到已有表达，不得重复整段用户画像或创建同名 H1 区块
- 已有内容用 edit；只有真正独立的新内容才用 insert_after。两者都必须先读取当前 Core，并使用精确且唯一的 old_text/anchor_text
- 冲突时用 view_core_memory 重新读取再重试，不要盲目追加、猜测或模糊删除
- batch 在同一份 Core 快照上原子执行；超出预算会拒绝，不会截断
- 管理类操作（recall / list / delete / stats / view_core / trash）按需通过 `request_tools(skills=["memory"])` 申请
