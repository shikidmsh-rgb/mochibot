<div align="center">

[English](README.md) | [中文](README.zh-CN.md)

# 🍡 MochiBot

**An open-source AI companion that remembers you, checks in on you, and grows with you.**

*Not just a chatbot — a companion that cares.*

**For people who want an AI that feels like a friend, not a search bar.**<br>
Emotional support. Daily check-ins. Gentle reminders. Always-on memory. Fully private.

</div>

---

## Why MochiBot

- **Lightweight** — single process, SQLite, no Docker/Redis/Postgres. `pip install` and go
- **Persistent memory** — 3-layer memory that survives restarts and self-organizes nightly, with full-text search and optional vector search
- **Proactive** — a heartbeat loop that checks in on you, not just waits for input
- **Private** — fully self-hosted, your data never leaves your machine
- **Extensible** — drop-in skills & observers, auto-discovered at startup. Skills support rich metadata, usage rules, and flexible triggers
- **Cost-efficient** — 5-tier model routing: use cheap models for simple tasks, powerful models only when needed
- **Body-aware** — built-in [Oura Ring](https://ouraring.com) integration: sleep, readiness, activity, stress — your bot notices what your words don't say

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

An autonomous background loop, not a cron job:

| Phase | What happens | LLM calls |
|-------|-------------|-----------|
| **Observe** | Collect world context from all observers (time, weather, activity, wearables) | 0 |
| **Think** | LLM evaluates: should I reach out? (delta detection — only fires when something changed) | 0–1 |
| **Act** | Send a proactive message, save an observation, or — most often — do nothing | 0 |

Rate-limited and conservative. A companion, not a spammer.

### 5-Tier Model Routing

MochiBot routes different tasks to the right model for the job — from cheap/fast for simple tool calls to powerful for deep analysis:

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  LITE             CHAT            DEEP                          │
│  ┌────────────┐   ┌────────────┐  ┌────────────┐               │
│  │ Simple tool │   │ Conver-    │  │ Complex    │               │
│  │ tasks       │   │ sations    │  │ analysis   │               │
│  └────────────┘   └────────────┘  └────────────┘               │
│                                                                 │
│  BG_FAST                          BG_DEEP                       │
│  ┌────────────┐                   ┌────────────┐               │
│  │ Background  │                   │ Background │               │
│  │ tagging     │                   │ reasoning  │               │
│  └────────────┘                   └────────────┘               │
│                                                                 │
│  Each tier: TIER_{name}_PROVIDER / API_KEY / MODEL / BASE_URL   │
│  Unconfigured tiers fall back to CHAT_* (zero-config = works)   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

| Tier | Purpose | Example use |
|------|---------|-------------|
| **LITE** | Cheap/fast | Simple tool tasks (check-ins, reminders) |
| **CHAT** | Balanced (default) | Daily conversations, proactive messages |
| **DEEP** | Powerful | Code analysis, complex reasoning |
| **BG_FAST** | Cheap background | Classification, tagging, summarization |
| **BG_DEEP** | Strong background | Heartbeat reasoning, memory operations |

**Backward compatible**: set `TIER_ROUTING_ENABLED=false` (default) and the system uses the original 2-model setup (Chat + Think). Enable tier routing when you're ready to optimize costs.

### Observers & Skills

| Concept | Role | Examples |
|---------|------|---------|
| **Observers** | Passive sensors that feed context into Think — zero LLM calls, interval-throttled | `time_context`, `weather`, `activity_pattern`, `oura` (sleep/readiness/stress) |
| **Skills** | Active capabilities the Chat model can invoke via tool calls — auto-discovered from `SKILL.md` + `handler.py` | `memory`, `reminder`, `todo`, `oura` |

Both are **auto-discovered at startup** — drop a folder, restart, done. Skills support two SKILL.md formats (v1 and v2) with rich metadata: type, multi-turn, usage rules, and flexible trigger configuration. See [CONTRIBUTING.md](CONTRIBUTING.md) for how to create your own.

---

## Quick Start

**Prerequisites**: Python 3.11+, an LLM API key, a [Telegram bot token](https://core.telegram.org/bots#how-do-i-create-a-bot)

```bash
git clone https://github.com/mochi-bot/mochibot.git && cd mochibot
cp .env.example .env        # then fill in CHAT_API_KEY, CHAT_MODEL, TELEGRAM_BOT_TOKEN
pip install -r requirements.txt
python -m mochi.main
```

Open Telegram → find your bot → send any message. The first person to message becomes the owner.

Two built-in debug commands: `/cost` shows LLM token usage for today and this month, `/heartbeat` shows the last heartbeat timestamp and what the bot decided to do.

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

The heartbeat runs continuously. **If you run on a laptop, the bot goes offline when you close the lid.**

| Option | Uptime | Cost |
|--------|--------|------|
| **Cloud VM** (Azure, AWS, etc.) | 24/7 | ~$4–10/mo |
| **Raspberry Pi / Mini PC** | 24/7 (home network) | One-time |
| **Laptop** | When open | Free |

> A small VM (1 vCPU, 1 GB RAM) is more than enough — single process, SQLite, minimal resources.

---

## Configuration

All config lives in `.env`. Key variables:

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `CHAT_PROVIDER` | `openai` | SDK: `openai` (+ any compatible), `azure_openai`, `anthropic` |
| `CHAT_API_KEY` | — | Your API key |
| `CHAT_MODEL` | — | Model for conversations (required) |
| `CHAT_BASE_URL` | — | Custom endpoint for OpenAI-compatible APIs |
| `THINK_MODEL` | *=CHAT* | Cheaper model for heartbeat + maintenance (optional) |
| `THINK_PROVIDER` | *=CHAT* | Separate provider for Think (optional) |
| `TELEGRAM_BOT_TOKEN` | — | From @BotFather |

### Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `HEARTBEAT_INTERVAL_MINUTES` | `20` | Observe → Think → Act cycle |
| `AWAKE_HOUR_START` / `END` | `7` / `23` | Heartbeat sleeps outside these hours |
| `MAX_DAILY_PROACTIVE` | `10` | Rate limit for proactive messages |
| `MAINTENANCE_HOUR` | `3` | Nightly maintenance (local time) |
| `TIMEZONE_OFFSET_HOURS` | `0` | Your UTC offset |

### 5-Tier Model Routing (optional)

Set `TIER_ROUTING_ENABLED=true` to enable. Each tier has four keys:

```
TIER_{LITE,CHAT,DEEP,BG_FAST,BG_DEEP}_{PROVIDER,API_KEY,MODEL,BASE_URL}
```

Unconfigured tiers fall back to `CHAT_*` / `THINK_*`. Zero-config = original 2-model behavior.

### Embedding & Vector Search (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `AZURE_EMBEDDING_ENDPOINT` | — | Azure OpenAI endpoint for embeddings |
| `AZURE_EMBEDDING_API_KEY` | — | Embedding API key |
| `AZURE_EMBEDDING_DEPLOYMENT` | — | Deployment name (e.g. `text-embedding-3-small`) |
| `VEC_SEARCH_NATIVE_ENABLED` | `false` | Enable sqlite-vec native vector KNN search |
| `RECALL_VEC_SIM_THRESHOLD` | `0.6` | Minimum cosine similarity for vector recall |

### Integrations (optional)

| Variable | Description |
|----------|-------------|
| `OURA_CLIENT_ID` | Oura Ring OAuth2 client ID (run `python oura_auth.py` to set up) |

See [.env.example](.env.example) for the full list.

**Dual-model example** — save tokens by using a cheaper Think model:

```dotenv
CHAT_MODEL=gpt-4o            # smart model for conversations
THINK_MODEL=gpt-4o-mini      # fast model for heartbeat + maintenance
```

**5-tier example** — fine-grained cost optimization:

```dotenv
TIER_ROUTING_ENABLED=true

TIER_LITE_MODEL=gpt-4o-mini       # cheap/fast for simple tool tasks
TIER_CHAT_MODEL=gpt-4o            # balanced for conversations
TIER_DEEP_MODEL=o3                 # powerful for complex analysis
TIER_BG_FAST_MODEL=gpt-4o-mini    # cheap for background tagging
TIER_BG_DEEP_MODEL=gpt-4o         # strong for background reasoning
```

---

## Customization

| I want to change... | Edit |
|---------------------|------|
| Personality, tone, name | `prompts/personality.md` |
| What gets remembered | `prompts/memory_extract.md` |
| When to proactively message | `prompts/think_system.md` |
| Morning / evening reports | `prompts/report_morning.md` / `report_evening.md` (disabled by default — enable via `MORNING_REPORT_HOUR` / `EVENING_REPORT_HOUR` in `.env`) |
| Observer intervals | `OBSERVATION.md` in each observer directory |
| Add a new skill or observer | See [CONTRIBUTING.md](CONTRIBUTING.md) |

> `prompts/personality.md` is the single most impactful file — it defines both how Mochi talks (`## Chat`) and what the heartbeat pays attention to (`## Think`).

---

## Best Practices

- **Deploy on a VM** — the heartbeat needs 24/7 uptime to be a true companion
- **Connect an Oura Ring** — run `python oura_auth.py` to authorize, then sleep/readiness/activity/stress data feeds into the heartbeat automatically. The built-in `oura` observer + skill handle everything
- **Use a cheaper Think model** — heartbeat and maintenance don't need your smartest model. Or enable 5-tier routing for fine-grained cost control (see [5-Tier Model Routing](#5-tier-model-routing))
- **Start with `prompts/personality.md`** — customizing your bot's voice matters more than any config variable
- **Start with built-in observers** before writing custom ones — time, activity, and weather provide a solid baseline

---

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

```
┌─────────────────────────────────┐
│ L1: Identity (prompts)          │  ← Your bot's personality
├─────────────────────────────────┤
│ L2: Config (.env → config.py)   │  ← 80+ tunables
├─────────────────────────────────┤
│ L3: Skills + Observers          │  ← Auto-discovered capabilities + sensors
├─────────────────────────────────┤
│ L4: Model Pool (5-tier routing) │  ← LLM orchestration
├─────────────────────────────────┤
│ L5: Core (DB + orchestration)   │  ← SQLite (22+ tables, FTS5, vector search)
└─────────────────────────────────┘
```

## Roadmap

- [x] Any OpenAI-compatible API (DeepSeek, Ollama, Groq, etc.)
- [x] Dual-model architecture (Chat + Think)
- [x] 5-tier model routing (lite / chat / deep / bg_fast / bg_deep)
- [x] Skill v2 system — rich metadata, usage rules, multi-turn, flexible triggers
- [x] Expanded DB schema — 22+ tables, FTS5 full-text search, optional sqlite-vec vector search
- [x] Embedding support — Azure OpenAI embeddings with TTL cache
- [ ] Morning / evening reports (scaffolded, enable via `MORNING_REPORT_HOUR` / `EVENING_REPORT_HOUR`)
- [x] Oura Ring integration — sleep, readiness, activity, stress (observer + skill)
- [ ] Pre-router — automatic skill selection before LLM call
- [ ] Tool governance — per-skill approval policies, audit logging
- [ ] Admin portal — web UI for memory inspection, config, and diagnostics
- [ ] Voice message support
- [ ] Multi-user support

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add skills, observers, and contribute to the framework.

## License

MIT — see [LICENSE](LICENSE)

---

<div align="center">

*Built with the belief that AI should be warm, not just smart.*

🍡

</div>
