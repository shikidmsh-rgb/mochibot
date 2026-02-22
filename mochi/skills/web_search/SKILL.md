---
name: web_search
description: "网络搜索 — 在线查找最新信息（无需 API 密钥）"
type: tool
tier: chat
expose_as_tool: true
---

# Web Search Skill

## Tools

### web_search (L0)
Search the internet using DuckDuckGo. Use for current events, news, prices, facts, how-to.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | yes | Search query. Use the language most likely to get good results. |
| max_results | integer | no | Max number of results to return (1-10, default 5). |

## Usage Rules
- Use this tool when the user asks about current events, recent news, prices, or any question that needs up-to-date information.
- After getting search results, summarize the findings in a clear, concise answer.
- If results are empty or a bot challenge is hit, tell the user the search failed and suggest rephrasing.
