# MochiBot Architecture

> Design reference for contributors. Read before writing code.

---

## Layer Architecture

```
┌─────────────────────────────────────────────────────┐
│  L1: Identity — system prompt, personality          │
│  → prompts/*.md                                     │
│  → Customize freely, this is YOUR bot's soul        │
├─────────────────────────────────────────────────────┤
│  L2: Config — thresholds, schedules, limits         │
│  → .env / config.py                                 │
│  → Tunable without code change                      │
├─────────────────────────────────────────────────────┤
│  L3: Skills — modular capabilities                  │
│  → mochi/skills/*/ (SKILL.md + handler.py)          │
│  → Self-contained, auto-discovered                  │
├─────────────────────────────────────────────────────┤
│  L3.5: Observers — passive world sensors            │
│  → mochi/observers/*/ (OBSERVATION.md + observer.py)│
│  → Read-only, interval-throttled, auto-discovered   │
├─────────────────────────────────────────────────────┤
│  L4: Core — orchestration, transport, infra         │
│  → mochi/*.py — glue code                           │
└─────────────────────────────────────────────────────┘
```

**Dependency direction (one-way, never reverse):**
```
Transport (telegram, discord)
  → Orchestration (heartbeat, ai_client)
    → Skills (mochi/skills/*)
      ← Core (memory_engine, db, llm, config)
```

---

## Codebase Map

```
mochi/
├── main.py               # Entry point — boots all subsystems
├── ai_client.py          # LLM chat + tool dispatch loop
├── llm.py                # LLM provider abstraction (OpenAI/Azure/Anthropic)
├── heartbeat.py          # Observe → Think → Act autonomous loop
├── memory_engine.py      # 3-layer memory (extract, dedup, rebuild)
├── prompt_loader.py      # Prompt hot-reload from prompts/*.md
├── runtime_state.py      # In-memory shared state
├── db.py                 # SQLite database layer (messages, reminders, todos, habits)
├── config.py             # Environment config (~30 settings)
├── transport/
│   ├── __init__.py       # Abstract Transport base class
│   └── telegram.py       # Telegram Bot API transport
├── observers/
│   ├── __init__.py       # Observer registry + discover() + collect_all()
│   ├── base.py           # Observer ABC + ObserverMeta + safe_observe() cache
│   ├── oura/             # Oura Ring (sleep, readiness, activity, stress)
│   ├── weather/          # Weather data (OpenWeatherMap)
│   └── habit/            # Habit tracking (SQLite)
└── skills/
    ├── __init__.py       # Skill registry + auto-discovery
    ├── base.py           # Skill base class + SKILL.md parser
    ├── memory/           # Save, recall, update core memory
    ├── oura/             # Oura Ring data tool (get_oura_data)
    ├── reminder/         # Time-based reminders
    └── todo/             # Todo list management

prompts/                  # Editable prompt templates
├── system_chat.md        # Bot personality
├── think_system.md       # Heartbeat decision prompt
├── memory_extract.md     # Memory extraction prompt
├── report_morning.md     # Morning briefing
└── report_evening.md     # Evening reflection

```

---

## Three-Layer Memory

```
Layer 1: Core Memory (compact summary, always in system prompt, ~800 tokens)
    ↑ rebuilt nightly from Layer 2
Layer 2: Memory Items (extracted facts, preferences, events — searchable)
    ↑ extracted from Layer 3 by LLM
Layer 3: Conversation History (raw messages — ephemeral, compressed over time)
```

**Cycle**: Chat → Extract (L3→L2) → Rebuild (L2→L1) → Deduplicate → Compress

---

## Heartbeat Loop

```
Observe (every 20min, 0 LLM calls)
  → Collect soft context: time, silence, user status, todos, reminders
  → collect_all() runs enabled observers in parallel (each at own interval)
  → obs["observers"] = {weather: {...}, habit: {...}, ...}
  → Delta detection: did anything change since last observe?

Think (on delta or 60min fallback, 1 LLM call)
  → LLM receives full obs dict including observers data
  → LLM decides: notify | save_memory | nothing
  → Rate-limited: max N/day, cooldown between messages

Act (execute decision)
  → Send proactive message via transport
  → Or save observation to memory
  → Or do nothing (most common)
```

**Key principle**: Observe is cheap (no LLM), Think is selective, Act is conservative.

---

## Skill System

Skills are self-contained modules discovered at startup.

### Structure
```
mochi/skills/{name}/
├── SKILL.md       # Tool definitions + metadata (REQUIRED)
├── handler.py     # Skill class with execute() (REQUIRED)
└── __init__.py    # (REQUIRED, can be empty)
```

### SKILL.md Format
```markdown
---
name: my_skill
expose: true
triggers: [tool_call]
---

## Tool: my_tool_name
Description: What this tool does

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| action | string | yes | What to do |
```

### Adding a Skill
1. Create the directory + three files
2. Restart MochiBot
3. Check logs for `✅ Registered skill: {name}`

### Disabling a Skill
Rename `SKILL.md` → `SKILL.md.disabled`

---

## Observer System

Observers are **read-only, interval-throttled sensors** that feed real-world context into the Heartbeat loop. They are passive — they never send messages or call skills.

### Structure
```
mochi/observers/{name}/
├── OBSERVATION.md   # Metadata + interval (REQUIRED)
├── observer.py      # Observer class with observe() (REQUIRED)
└── __init__.py      # (REQUIRED, can be empty)
```

### OBSERVATION.md Format
```markdown
---
name: my_observer
interval_minutes: 30
enabled: true
requires_config: [MY_API_KEY, MY_API_SECRET]
---

Description of what this observer watches.
```

### Key Behaviours
- **Auto-disabled** if any `requires_config` env var is missing at startup
- **Interval-throttled**: `safe_observe()` skips call if last run < `interval_minutes` ago
- **Error-isolated**: exceptions are caught and logged; observer returns `{}` on failure
- **5 consecutive failures** → auto-disabled for the session
- **Empty `{}` results are omitted** from `obs["observers"]`

### Adding an Observer
1. Create the directory + three files
2. Restart MochiBot
3. Check logs for `✅ Registered observer: {name}`

### Disabling an Observer
Set `enabled: false` in `OBSERVATION.md`, or rename it `OBSERVATION.md.disabled`.

---

## Key Rules

1. **Transport = dumb pipe** — no business logic in transport files
2. **Dependency direction** — never import upward (skill → heartbeat = forbidden)
3. **Config, don't hardcode** — thresholds, timings, limits go in .env
4. **Skills are self-contained** — each skill is its own world
5. **Memory is sacred** — don't bypass the 3-layer architecture
6. **Observers are read-only** — never send messages, never call skills, no side effects
7. **Observers use `safe_observe()`** — always let the base cache handle interval throttling
