---
name: web_search
description: "网络搜索 — 支持 Tavily、百度千帆与 Bing"
type: tool
config:
  TAVILY_API_KEY:
    type: str
    secret: true
    default: ""
    description: "Optional Tavily Search API key"
  BAIDU_API_KEY:
    type: str
    secret: true
    default: ""
    description: "Optional Baidu Qianfan AI Search API key"
---

# Web Search Skill

## Tools

### web_search (routed)
搜索互联网，查询时事、新闻、价格、知识、教程等。配置 Tavily API Key 后使用 Tavily；否则沿用百度千帆优先、Bing 备用。Bing 不支持时间范围筛选，切换时结果会注明。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | yes | 搜索关键词。使用最可能获得好结果的语言。 |
| max_results | integer | no | 最大返回结果数（1-10，默认 5） |
| recency | string (enum: week, month, semiyear, year) | no | 可选的结果时间范围，Tavily 和百度支持 |
