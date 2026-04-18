---
name: note
description: "便签/备忘 — 记一下、留个笔记、稍后提醒、条件型观察（写入 notes.md）"
type: tool
tier: lite
expose_as_tool: true
always_on: true
core: true
---

# Note Skill

便签纸。写在这里的内容，heartbeat 每次巡逻（~20min）都会看到。
适合：口头交代（"遛狗后再做"）、条件型观察（"压力高时提醒我"）、软提醒、临时备忘、长期规则。

**不适合**：精确时间提醒（用 reminder）、定期打卡（用 habit）。

## Tools

### manage_note (L0)
备忘条。写在这里的东西 heartbeat 每次巡逻都会看到，帮助记住该怎么陪伴用户——状态、心情、偏好、需要关注的事。写了 note = 系统会持续留意、主动关心、随时提醒。

举例：用户说"下午心情不好"→ 记下来，巡逻时多关心；用户说"明天有考试别烦我"→ 记下来，明天调整互动方式。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, list, remove, rewrite) | yes | add / list / remove / rewrite |
| content | string | | Note text. Required for add. |
| note_id | integer | | Required for remove. Line number from list output. |
| notes | array | | Required for rewrite. Complete replacement list — old notes are discarded. |

## Usage Rules

- 判断标准：**下次巡逻时系统需要记得这件事来更好地陪伴用户吗？** 需要就记
- **条件型交代**（"遛狗后再做XX"）→ note（不是定时器）
- **修改已有 habit/todo 的执行条件** → 用 note，不要创建 reminder
