<div align="center">

[English](README.md) | [中文](README.zh-CN.md)

# 🍡 MochiBot

**Open-source AI companion bot with persistent memory and proactive check-ins.**

</div>

---

## Features

- **Lightweight** — single process, SQLite, no Docker/Redis/Postgres. `pip install` and go
- **Persistent memory** — 3-layer memory that survives restarts and self-organizes nightly (full-text search + optional vector search)
- **Proactive** — heartbeat loop that checks in on you, not just waits for input
- **Self-hosted** — your data stays on your machine
- **Extensible** — drop-in skills & observers, auto-discovered at startup
- **Cost-efficient** — 5-tier model routing: cheap models for simple tasks, powerful models only when needed
- **Body-aware** — [Oura Ring](https://ouraring.com) integration for sleep, readiness, activity, stress

---

## Design

### Three-Layer Memory

```
Layer 1: Core Memory    — compact summary, always in system prompt (~800 tokens)
    ↑ rebuilt nightly from Layer 2
Layer 2: Memory Items   — extracted facts, preferences, events (searchable)
    ↑ extracted from Layer 3 by LLM
Layer 3: Conversations  — raw messages, compressed over time
```

Every night: extract → deduplicate → rebuild core summary → compress old conversations.

### Heartbeat (Observe → Think → Act)

Background loop:

| Phase | What happens | LLM calls |
|-------|-------------|-----------|
| **Observe** | Collect context from observers (time, weather, activity, wearables) | 0 |
| **Think** | LLM evaluates: should I reach out? (delta detection — only on change) | 0–1 |
| **Act** | Send a proactive message, save an observation, or do nothing | 0 |

Rate-limited and conservative.

### 5-Tier Model Routing

| Tier | Purpose | Example |
|------|---------|---------|
| **LITE** | Cheap/fast | Tool tasks (check-ins, reminders) |
| **CHAT** | Balanced (default) | Conversations, proactive messages |
| **DEEP** | Powerful | Code analysis, complex reasoning |
| **BG_FAST** | Cheap background | Classification, tagging, summarization |
| **BG_DEEP** | Strong background | Heartbeat reasoning, memory ops |

Unconfigured tiers fall back to `CHAT_*`. Set `TIER_ROUTING_ENABLED=false` (default) to use the 2-model setup (Chat + Think).

### Pre-Router & Tool Governance

Selectively injects skills per message to keep token costs low:

1. **Pre-Router** — LLM classifies the message and selects which skills to load
2. **Keyword Fallback** — catches obvious cases if the pre-router misses
3. **Tool Escalation** — LLM can request missing skills mid-turn via `request_tools`

Tool policy layer gates every call with check/filter/rate-limit.

### Diary (Working Memory)

Daily scratchpad shared between Chat and Think — observations, notes, context that don't fit into long-term memory. Auto-archived nightly.

### Observers & Skills

| Concept | Role | Examples |
|---------|------|---------|
| **Observers** | Passive sensors feeding context into Think — zero LLM calls, interval-throttled | `time_context`, `weather`, `activity_pattern`, `oura` |
| **Skills** | Active capabilities invoked via tool calls — auto-discovered from `SKILL.md` + `handler.py` | `memory`, `reminder`, `todo`, `diary`, `oura` |

Both are auto-discovered at startup — drop a folder, restart, done. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Quick Start

**Prerequisites**: Python 3.11+, an LLM API key, a [Telegram bot token](https://core.telegram.org/bots#how-do-i-create-a-bot)

```bash
git clone https://github.com/mochi-bot/mochibot.git && cd mochibot
cp .env.example .env        # fill in CHAT_API_KEY, CHAT_MODEL, TELEGRAM_BOT_TOKEN
pip install -r requirements.txt
python -m mochi.main
```

Open Telegram → find your bot → send any message. The first person to message becomes the owner.

Debug commands: `/cost` (token usage), `/heartbeat` (last heartbeat status).

> **Any OpenAI-compatible API works.** Set `CHAT_BASE_URL` to point at your provider:
>
> | Provider | `CHAT_BASE_URL` | Example `CHAT_MODEL` |
> |----------|-----------------|----------------------|
> | OpenAI (default) | *(not needed)* | `gpt-4o` |
> | DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
> | Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
> | Ollama (local) | `http://localhost:11434/v1` | `llama3` |

---

## Deployment

The heartbeat runs continuously — if you run on a laptop, the bot goes offline when you close the lid.

| Option | Uptime | Cost |
|--------|--------|------|
| **Cloud VM** (Azure, AWS, etc.) | 24/7 | ~$4–10/mo |
| **Raspberry Pi / Mini PC** | 24/7 (home network) | One-time |
| **Laptop** | When open | Free |

> A small VM (1 vCPU, 1 GB RAM) is enough — single process, SQLite, minimal resources.

---

## Configuration

All config lives in `.env`. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CHAT_PROVIDER` | `openai` | SDK: `openai` (+ compatible), `azure_openai`, `anthropic` |
| `CHAT_API_KEY` | — | Your API key |
| `CHAT_MODEL` | — | Model for conversations (required) |
| `CHAT_BASE_URL` | — | Custom endpoint for OpenAI-compatible APIs |
| `THINK_MODEL` | *=CHAT* | Cheaper model for heartbeat + maintenance (optional) |
| `TELEGRAM_BOT_TOKEN` | — | From @BotFather |
| `HEARTBEAT_INTERVAL_MINUTES` | `20` | Observe → Think → Act cycle |
| `AWAKE_HOUR_START` / `END` | `7` / `23` | Heartbeat sleeps outside these hours |
| `MAX_DAILY_PROACTIVE` | `10` | Rate limit for proactive messages |
| `TIMEZONE_OFFSET_HOURS` | `0` | Your UTC offset |

<details>
<summary>Advanced: 5-tier routing, pre-router, embeddings, integrations</summary>

**5-tier routing** — set `TIER_ROUTING_ENABLED=true`, then configure each tier:

```
TIER_{LITE,CHAT,DEEP,BG_FAST,BG_DEEP}_{PROVIDER,API_KEY,MODEL,BASE_URL}
```

**Pre-Router** — `TOOL_ROUTER_ENABLED=true` enables LLM-based skill selection per message. `TOOL_ESCALATION_ENABLED=true` (default) allows mid-turn skill requests.

**Embeddings** — `AZURE_EMBEDDING_ENDPOINT`, `AZURE_EMBEDDING_API_KEY`, `AZURE_EMBEDDING_DEPLOYMENT`

**Oura Ring** — `OURA_CLIENT_ID`, `OURA_CLIENT_SECRET` (run `python oura_auth.py` to set up)

See [.env.example](.env.example) for key tunables; see `mochi/config.py` for the full list of ~70 tunables.

</details>

**Example** — dual-model setup:

```dotenv
CHAT_MODEL=gpt-4o            # conversations
THINK_MODEL=gpt-4o-mini      # heartbeat + maintenance
```

---

## Customization

| I want to change... | Edit |
|---------------------|------|
| Personality, tone, name | `prompts/personality.md` |
| What gets remembered | `prompts/memory_extract.md` |
| When to proactively message | `prompts/think_system.md` |
| Morning / evening reports | `prompts/report_morning.md` / `report_evening.md` (off by default — set `MORNING_REPORT_HOUR` / `EVENING_REPORT_HOUR`) |
| Observer intervals | `OBSERVATION.md` in each observer directory |
| Add a skill or observer | See [CONTRIBUTING.md](CONTRIBUTING.md) |

> `prompts/personality.md` is the most impactful file — it defines how the bot talks and what the heartbeat pays attention to.

---

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md).

## Roadmap

- [x] Any OpenAI-compatible API (DeepSeek, Ollama, Groq, etc.)
- [x] Dual-model architecture (Chat + Think)
- [x] 5-tier model routing
- [x] Skill v2 — rich metadata, usage rules, multi-turn, flexible triggers
- [x] Expanded DB — 22+ tables, FTS5, optional sqlite-vec
- [x] Embedding support (Azure OpenAI + TTL cache)
- [x] Oura Ring integration (observer + skill)
- [x] Pre-router — automatic skill selection
- [x] Tool governance — policy check, filter, rate limiter
- [x] Diary system — daily working memory + nightly archive
- [x] Nightly maintenance — dedup, stale demotion, core memory audit
- [x] Modular prompt assembly
- [x] Chatty rhythm — multi-bubble + typing indicators
- [x] Morning / evening reports
- [ ] Admin portal (web UI)
- [ ] Voice message support
- [ ] Multi-user support

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE)
