---
name: note
description: "便签/备忘 — 记一下、留个笔记、稍后提醒、条件型观察 (写入 notes.md)"
type: tool
tier: lite
expose_as_tool: true
---

# Note Skill

工作笔记。写在这里的内容，heartbeat 每次巡逻（~20min）都会看到。
适合：口头交代（"遛狗后再做"）、条件型观察（"压力高时提醒我"）、软提醒、临时备忘。

**不适合**：精确时间提醒（用 reminder）、定期打卡（用 habit）。

## Tools

### manage_note (L0)
Add, list, or remove notes from the working notepad.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, list, remove) | yes | add / list / remove |
| content | string | | Note text. Required for add. |
| note_id | integer | | Required for remove. Line number from list output. |

## Usage Rules

- **"记一下" / "帮我记住" / "留个备注"** → `manage_note` add
- **"遛狗后再做XX"** → add a note (条件型，不是定时器)
- Notes are persistent — Think reads them every patrol cycle
- If the user's request is about **modifying when/how to do an existing habit or todo**, use a note instead of creating a reminder
- **"我每天早上需要更多关怀"** → add as a note (Think will read it and adjust behavior)
- **"晚上11点以后疯狂提醒我睡觉"** → add as a note (Think will see it at night and act)
