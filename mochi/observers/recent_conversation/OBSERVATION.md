---
name: recent_conversation
interval: 20
enabled: true
requires_config: []
---

Last ~10 conversation rounds (20 messages) from SQLite.
Zero external calls. Gives the heartbeat Think step conversational context
so it can refer to what was actually discussed, not just silence duration.

## Fields
| Field | Type | Description |
|-------|------|-------------|
| messages | list | Compact message list: [{role, content, when}] |
| count | number | Number of messages returned |
| last_user_message | string | The last thing the user said (truncated to 200 chars) |
| last_user_message_when | string | Relative time of last user message ("2h ago", "just now") |

## Message entry fields
| Field | Type | Description |
|-------|------|-------------|
| role | string | "user" or "assistant" |
| content | string | Message text, truncated to 200 chars if longer |
| when | string | Relative timestamp ("5m ago", "3h ago", "2d ago") |

## Token budget
Each message entry is capped at 200 chars. With 20 messages that's roughly
~1,000-1,500 tokens — modest overhead for a significant context improvement.

## Notes
- interval=20 matches heartbeat frequency — always shows the latest exchange
- Results are ordered oldest → newest (natural reading order)
- Empty if no owner set or no messages yet
