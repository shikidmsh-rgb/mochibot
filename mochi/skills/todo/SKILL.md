---
name: todo
description: "一次性待办 — 追踪到完成后即结束的事项（如买猫粮、约牙医、查资料），也承载不属于活跃习惯的一次性完成回报。"
type: tool
multi_turn: true
diary_status_order: 20
sense:
  interval: 20
---

# Todo Skill

## Capability Context

- todo 持续保留一次性事项的未完成状态，直到完成或删除；完成后不再重复出现，reopen 可恢复为未完成。
- `nudge_date` 是再次关注日期，到期会让 Observer 看见这个事项。它不是精确到点通知，也不保证届时主动发送。
- 今日状态中的待办带有 `[todo_id=X]`，这个 ID 可直接用于完成、重新打开、更新或删除，操作结果会返回真实回执。
- 原文匹配只统一全角半角、大小写与空白，不做关键词或语义匹配；零个或多个候选时不会修改待办。

## Tools

### manage_todo (routed)
创建、查看、完成、重新打开、更新或删除需要持续追踪的一次性事项，例如买猫粮、交报告或已经完成的 PR。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, list, complete, reopen, update, delete) | yes | 要执行的操作 |
| task | string | | add 的任务描述；update 时为新的完整任务描述 |
| todo_id | integer | | complete/reopen/update 可用；delete 必须使用 ID；与 match 同时提供时以 ID 为准 |
| match | string | | complete/reopen/update 未给 ID 时，可提交待办原文；仅唯一规范化精确匹配才执行 |
| nudge_date | string | | add/update 的再次关注日期（YYYY-MM-DD），不是精确到点通知 |
| clear_nudge_date | boolean | | update 时设为 true，显式清除再次关注日期；不能同时提供 nudge_date |
| include_done | boolean | | 是否包含已完成项（list 用），默认 false |
