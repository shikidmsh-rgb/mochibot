---
name: web_search
description: "Web search — find current information online (no API key needed)"
type: tool
tier: chat
expose_as_tool: true
config:
  WEB_SEARCH_TIMEOUT_S:
    type: int
    default: 20
    description: "HTTP timeout in seconds for DuckDuckGo requests"
  WEB_SEARCH_MAX_RESULTS:
    type: int
    default: 5
    description: "Max results to return per search"
  WEB_SEARCH_CACHE_TTL_S:
    type: int
    default: 300
    description: "Cache TTL in seconds for search results"
  WEB_SEARCH_CACHE_SIZE:
    type: int
    default: 256
    description: "Max number of cached search results"
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
