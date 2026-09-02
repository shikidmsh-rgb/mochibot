---
name: web_search
description: "网络搜索 — 在线查找最新信息（无需 API 密钥）"
type: tool
---

# Web Search Skill

## Tools

### web_search (routed)
通过 Bing 搜索互联网。用于查询时事、新闻、价格、知识、教程等。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | yes | 搜索关键词。使用最可能获得好结果的语言。 |
| max_results | integer | no | 最大返回结果数（1-10，默认 5） |
