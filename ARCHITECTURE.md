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
│  → .env / config.py (~80 tunables)                  │
│  → Tunable without code change                      │
├─────────────────────────────────────────────────────┤
│  L3: Skills + Observers                             │
│  → mochi/skills/*/ (SKILL.md + handler.py)          │
│  → mochi/observers/*/ (OBSERVATION.md + observer.py)│
│  → Self-contained, auto-discovered at startup       │
│  → Registry: SKILLS.md / OBSERVERS.md               │
├─────────────────────────────────────────────────────┤
│  L4: Model Pool — 5-tier LLM routing               │
│  → model_pool.py + llm.py                           │
│  → lite / chat / deep / bg_fast / bg_deep           │
├─────────────────────────────────────────────────────┤
│  L5: Core — DB, orchestration, transport            │
│  → SQLite (22+ tables, FTS5, optional sqlite-vec)   │
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
├── model_pool.py         # 5-tier model routing + embedding client (Azure OpenAI)
├── heartbeat.py          # Observe → Think → Act autonomous loop
├── memory_engine.py      # 3-layer memory (extract, dedup, outdated removal, salience rebalance)
├── prompt_loader.py      # Modular prompt assembly + hot-reload
├── runtime_state.py      # Thread-safe in-memory cross-module state
├── db.py                 # SQLite (22+ tables, FTS5, optional sqlite-vec)
├── config.py             # Environment config (~80 tunables)
├── oura_client.py        # Oura Ring OAuth2 + token refresh + API cache
├── tool_router.py        # Selective skill injection (LLM classify + keyword fallback)
├── tool_policy.py        # Tool governance (denylist, rate limit, confirm gate)
├── transport/
│   ├── __init__.py       # Abstract Transport base class
│   └── telegram.py       # Telegram Bot API transport
├── observers/
│   ├── __init__.py       # Observer registry + discover() + collect_all()
│   ├── base.py           # Observer ABC + ObserverMeta + safe_observe() cache
│   ├── time_context/     # Date, time-of-day, holidays, silence duration
│   ├── activity_pattern/ # 7-day conversation trend detection
│   ├── recent_conversation/ # Last 20 messages (context for Think)
│   ├── weather/          # Weather data (OpenWeatherMap)
│   ├── habit/            # Habit tracking (SQLite)
│   └── oura/             # Oura Ring (sleep, readiness, activity, stress)
└── skills/
    ├── __init__.py       # Skill registry + auto-discovery
    ├── base.py           # Skill base class + SKILL.md parser (v1 + v2)
    ├── memory/           # 8 tools: save, recall, update_core (add/delete), list, delete, stats, view_core, trash_bin
    ├── oura/             # get_oura_data (sleep, activity, readiness, stress)
    ├── reminder/         # manage_reminder (create/list/delete)
    ├── todo/             # manage_todo (add/list/complete/delete)
    ├── diary/            # read_diary, update_diary — daily working memory + auto-archive
    └── maintenance/      # run_maintenance — nightly pipeline (archive/dedup/outdated/salience/audit/trash)

data/                     # Runtime data (auto-created)
├── diary.md              # Today's diary entries (working file)
└── diary_archive/        # Monthly diary rollups (YYYY-MM.md)

prompts/                  # Editable prompt templates (modular)
├── system_chat.md        # Chat system prompt — imports modules below
├── system_chat/
│   ├── soul.md           # Bot personality / values core
│   ├── user.md           # User memory context module
│   ├── tools.md          # Tool usage rules
│   └── runtime_context.md # Runtime context (time, heartbeat info)
├── think_system.md       # Heartbeat decision prompt
├── memory_extract.md     # Memory extraction rules
├── report_morning.md     # Morning briefing (disabled by default)
└── report_evening.md     # Evening reflection (disabled by default)

tests/
├── e2e/                  # End-to-end tests (mock LLM + fake transport)
│   ├── mock_llm.py       # Deterministic LLM stub
│   ├── fake_transport.py # In-memory transport for testing
│   └── test_*.py         # E2E test suites
└── test_*.py             # Unit tests
```

---

## Three-Layer Memory

```
Layer 1: Core Memory (compact summary, always in system prompt, ~800 tokens)
    ↑ owned by chat model (add/delete lines via update_core_memory tool)
Layer 2: Memory Items (extracted facts, preferences, events — searchable)
    ↑ extracted from Layer 3 by LLM; importance ★1 routine / ★2 important / ★3 critical
Layer 3: Conversation History (raw messages — ephemeral, compressed over time)
```

**Cycle**: Chat → Extract (L3→L2) → Deduplicate → Outdated Removal → Salience Rebalance → Core Audit

**Memory Tools** (8 total, exposed to LLM):
- `save_memory` — manually save a fact
- `recall_memory` — search by keyword/category
- `update_core_memory` — add/delete lines in core memory
- `list_memories` — browse all memories (optional category filter)
- `delete_memory` — soft-delete to trash (recoverable 30 days)
- `memory_stats` — count, categories, trash size
- `view_core_memory` — display full core memory
- `memory_trash_bin` — list/restore deleted memories

**Soft-delete**: deleted items go to `memory_trash` table, kept 30 days, restorable. Purged by nightly maintenance.

---

## Heartbeat Loop

```
Observe (every 20min, 0 LLM calls)
  → Collect soft context: time, silence, user status, todos, reminders
  → collect_all() runs enabled observers in parallel (each at own interval)
  → obs["observers"] = {weather: {...}, habit: {...}, ...}
  → Collect diary snapshot + maintenance summary (if available)

Delta Detection (0 LLM calls)
  → Per-observer has_delta(prev, curr) — suppresses noisy sources
  → Checks maintenance summary arrival, upcoming reminders
  → Think fires only on delta OR 60min fallback

Think (on delta or 60min fallback, 1 LLM call)
  → LLM receives full obs dict including observers data
  → LLM decides: notify | save_memory | update_diary | nothing
  → Rate-limited: max N/day, cooldown between messages

Act (execute decision)
  → Send proactive message via transport
  → Or save observation to memory
  → Or append to diary
  → Or do nothing (most common)

Scheduled Tasks (run inside heartbeat cycle)
  → Morning / Evening reports (disabled by default)
  → Maintenance pipeline (at MAINTENANCE_HOUR, default 3 AM)
```

**Key principle**: Observe is cheap (no LLM), Think is selective, Act is conservative.

---

## Skill System

Skills are self-contained modules discovered at startup. See **[SKILLS.md](SKILLS.md)** for the full registry (name, description, status).

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

Observers are **read-only, interval-throttled sensors** that feed real-world context into the Heartbeat loop. They are passive — they never send messages or call skills. See **[OBSERVERS.md](OBSERVERS.md)** for the full registry (name, description, status, interval).

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
interval: 30
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
- **Delta detection**: each observer can implement `has_delta(prev, curr)` to suppress noisy Think triggers

### Adding an Observer
1. Create the directory + three files
2. Restart MochiBot
3. Check logs for `✅ Registered observer: {name}`

### Disabling an Observer
Set `enabled: false` in `OBSERVATION.md`, or rename it `OBSERVATION.md.disabled`.

---

## Tool Router

Selective tool injection to reduce token waste — instead of injecting ALL skill definitions into every LLM call, the router picks only relevant skills.

```
User message arrives
  → tool_router classifies intent (BG_FAST tier, ~100 tokens)
  → Returns subset of skill names
  → Only those tools are injected into the system prompt

Fallback: keyword matching (instant, 0 tokens)
  → Fires ONLY when LLM classification returns empty
  → Iron rule: keywords never union with LLM result
```

**Tool escalation**: if the LLM needs a tool mid-turn that wasn't injected, it can call the `request_tools` virtual tool to self-rescue and retry with the needed skills.

---

## Tool Governance

Lightweight policy layer (`tool_policy.py`) applied before any skill executes.

| Control | Config key | Behaviour |
|---------|-----------|-----------|
| **Denylist** | `TOOL_DENY_NAMES` | Tool call silently blocked, never reaches skill |
| **Rate limit** | `TOOL_RATE_LIMIT_PER_MIN` | Per-tool sliding window (default 10/min) |
| **Confirm gate** | `TOOL_REQUIRE_CONFIRM` | Placeholder — requires transport UX for user approval |

All controls are environment-configurable (`config.py`). Disabled by default.

---

## Diary System

A **daily working memory** that persists short-term context across conversations within a day.

```
data/diary.md              ← Today's entries (working file, max 30 lines)
data/diary_archive/
  └── YYYY-MM.md           ← Monthly rollups (auto-archived nightly)
```

- **Write**: Heartbeat Think can decide `update_diary` to append observations
- **Trim**: When exceeding 30 lines, oldest entries are trimmed to 25
- **Archive**: At `MAINTENANCE_HOUR`, today's diary is appended to the monthly archive and working file is cleared
- **Read**: Diary snapshot is included in the Observe phase for continuity

---

## Maintenance Pipeline

Nightly automated housekeeping, runs at `MAINTENANCE_HOUR` (default 3 AM).

```
1. Diary archive       — snapshot today's diary → monthly file, clear working file
2. Dedup               — merge near-duplicate memory items (uses LLM)
3. Outdated removal    — LLM identifies stale memories (passed deadlines, resolved issues, temp moods)
4. Salience rebalance  — promote frequently-accessed ★1→★2; demote abandoned ★2→★1 (LLM confirms)
5. Core audit          — verify core_memory is within token budget
6. Trash purge         — hard-delete trash items older than TRASH_PURGE_DAYS (default 30)
7. Summary             — store maintenance report for morning briefing
```

Triggered by heartbeat; results feed into the next morning report. Entire pipeline is skippable via `MAINTENANCE_ENABLED=false`.

---

## Modular Prompt System

System prompts are assembled from modules at runtime (hot-reloaded on each chat).

```
prompts/system_chat.md          ← Main chat prompt, imports:
  └── prompts/system_chat/
      ├── soul.md               ← Personality, values, tone
      ├── user.md               ← User memory context
      ├── tools.md              ← Tool usage rules + available tools
      └── runtime_context.md    ← Current time, heartbeat state, diary
```

Modules are `{% include %}` style — `system_chat.md` references each sub-file. This keeps the main prompt readable while allowing granular edits.

---

## Key Rules

1. **Transport = dumb pipe** — no business logic in transport files
2. **Dependency direction** — never import upward (skill → heartbeat = forbidden)
3. **Config, don't hardcode** — thresholds, timings, limits go in .env
4. **Skills are self-contained** — each skill is its own world
5. **Memory is sacred** — don't bypass the 3-layer architecture
6. **Observers are read-only** — never send messages, never call skills, no side effects
7. **Observers use `safe_observe()`** — always let the base cache handle interval throttling
8. **Tool router is additive** — skills not injected can still be requested mid-turn via escalation
9. **Maintenance is idempotent** — safe to re-run; skips steps that already completed

---

## Testing

```
tests/
├── test_*.py             # Unit tests (standard pytest)
└── e2e/
    ├── mock_llm.py       # Deterministic LLM stub (returns canned responses)
    ├── fake_transport.py  # In-memory transport (no network, captures messages)
    └── test_*.py          # E2E suites (chat flow, heartbeat flow, reminder delivery)
```

E2E tests boot the full stack with mock LLM + fake transport, verifying end-to-end behaviour without real API calls or Telegram.
