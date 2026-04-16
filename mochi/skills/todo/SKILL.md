---
name: todo
description: "一次性待办 — 需要被追踪催促直到完成，但做完就结束不会再来的事（如买猫粮、约牙医、查资料）。"
type: tool
expose_as_tool: true
diary_status_order: 20
writes:
  db: [todos]
sense:
  interval: 20
---

# Todo Skill

## Usage Rules

- `nudge_date`：用户暗示软截止日期时设置（"这周内搞定"、"月底前"），到期时系统会主动提醒
- 所有待办在日记"今日状態"中带 `[todo_id=X]`，可直接用该 ID 操作，不必先 list

## Tools

### manage_todo (L1)
管理待办：添加、列出、完成、删除或更新。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string | yes | add / list / complete / delete / update |
| task | string | | 任务描述（add 必填，update 时为新内容） |
| todo_id | integer | | 待办 ID（complete/delete/update 必填） |
| nudge_date | string | | 软提醒日期（YYYY-MM-DD），设置后系统会在该日期主动提醒（add/update 可用） |
| include_done | boolean | | 是否包含已完成项（list 用），默认 false |
