---
name: my_skill
description: "What this skill does — keep it concise, the pre-router reads this"
type: tool
expose_as_tool: true
keywords: [my_keyword, 我的关键词]
---

## Tools

### my_tool (L1)
Describe what this tool does.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string | yes | add / list / delete |
| content | string | | Item content (action=add) |
| item_id | integer | | Item ID (action=delete) |
