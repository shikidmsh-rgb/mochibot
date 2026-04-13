---
name: sticker
description: "贴纸 — send contextual stickers"
type: tool
expose_as_tool: true
tier: lite
exclude_transports: [wechat]
sub_skills:
  sticker_manage: "贴纸管理 — delete stickers from registry"
---

## Tools

### send_sticker (L0)
Send a sticker from the learned registry based on mood or semantic tags. Call this when a sticker would enhance the emotional expression of your response.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| mood | string | yes | The mood or semantic tag to match. Use Chinese tags like: 酷, 自信, 得意, 生气, 愤怒, 不爽, 崩溃, 头晕, 伤心, 大哭, 委屈, 难过, 困倦, 疲惫, 想睡觉. A random sticker is sent if no exact match. |

### delete_last_sticker (L0, skill: sticker_manage)
Delete the most recently sent sticker from the registry. Call this when the user expresses dislike for a sticker you just sent (e.g., "这个表情包不好看，删掉", "删掉这个贴纸", "这个表情不好").

No parameters required.

## Usage Rules
- Only call send_sticker when you genuinely want to express emotion — not as a default for every reply
- **ALWAYS include text alongside stickers** — a sticker alone is NEVER a complete reply. Even for greetings (早安/晚安), farewells, or simple reactions, you MUST write text too. The sticker enhances your words; it never replaces them.
- Send at most 1 sticker per reply turn
- The tool returns a special marker; do NOT describe the sticker in your text response
- Use Chinese tags for mood parameter — they match better with the learned registry
- Call delete_last_sticker only when the user explicitly asks to remove a sticker
