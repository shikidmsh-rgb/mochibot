# MochiBot Architecture

> Design reference for contributors. Read this before writing code.

---

## Design Principles

1. **Skill is the only capability unit.** Drop a folder in, it works. Delete it, it's gone.
2. **SKILL.md declares everything.** Framework reads the declaration, handles orchestration.
3. **Toggle a skill = toggle all its dimensions.** Tools, observer, diary — all follow the skill's on/off state.
4. **No cross-skill dependencies.** Disabling skill A must never crash skill B.
5. **Framework does the wiring.** Skills declare intent, framework orchestrates. Skills don't import framework internals.

---

## Architecture Overview

### 6 Components + DB

```
┌─ 1. Channel ─────────────────────────────────┐
│  Message transport: Telegram                   │
│  Dumb pipe — receive message, send reply       │
├─ 2. Core ─────────────────────────────────────┤
│  Static identity files (human-edited):         │
│  soul.md / user.md / tools.md / runtime_ctx.md │
│  "Who is the bot, who is the user"             │
├─ 3. Engine ───────────────────────────────────┤
│  Prompt Builder — assemble Core + Memory +     │
│    Skill descriptions into system prompt        │
│  Tool Router — classify intent, select skills   │
│  LLM Runner — call model + tool loop           │
├─ 4. Heartbeat ────────────────────────────────┤
│  Autonomous cycle: Observe → Think → Act       │
│  Observe: collect from observers               │
│  Think: LLM evaluates observations             │
│  Act: notify / save_memory / update_diary      │
├─ 5. Memory ───────────────────────────────────┤
│  Core Memory — always injected (~800 tok)      │
│  Memory Items — semantic search (embeddings)   │
│  Diary — short-term buffer (daily working mem)  │
│  Nightly maintenance: dedup/compress/forget     │
├─ 6. Skill ────────────────────────────────────┤
│  The ONLY capability unit.                     │
│  Dimensions: tool + observer + diary           │
│                                                │
│  DB — SQLite (shared infrastructure)           │
└────────────────────────────────────────────────┘
```

### Data Flow

```
User message
  → Channel (receive)
    → Engine (build prompt + route tools + LLM + tool loop)
      ← reads Core (identity)
      ← reads Memory (context)
      ← reads Skill declarations (available tools)
    → Channel (send reply)

Heartbeat (periodic, no user message)
  → Observe: call observe() on skills that declare observer
  → Think: Engine evaluates observations
  → Act: execute via skill tools or send proactive message
```

---

## Codebase Map

```
mochi/
├── main.py               # Entry point — boots all subsystems
├── ai_client.py          # Prompt build + LLM chat + tool dispatch loop
├── llm.py                # LLM provider abstraction (OpenAI/Azure/Anthropic)
├── model_pool.py         # 3-tier model routing + embedding client
├── heartbeat.py          # Observe → Think → Act autonomous loop + state persistence
├── diary.py              # DailyFile class + refresh_diary_status() (L4 infrastructure)
├── reminder_timer.py     # Precise reminder delivery (asyncio timer + recurrence)
├── memory_engine.py      # 3-layer memory (extract, dedup, outdated, salience)
├── prompt_loader.py      # Modular prompt assembly + hot-reload
├── runtime_state.py      # Thread-safe in-memory cross-module state
├── db.py                 # SQLite (22+ tables, FTS5, optional sqlite-vec)
├── config.py             # Environment config (~80 tunables)
├── skill_config_resolver.py # Skill config priority chain (DB > env > schema)
├── oura_client.py        # Oura Ring OAuth2 + token refresh + API cache
├── tool_router.py        # Selective skill injection + SSOT metadata + tier routing
├── tool_policy.py        # Tool governance (denylist, rate limit)
├── transport/
│   ├── __init__.py       # Abstract Transport base class
│   └── telegram.py       # Telegram Bot API transport
├── admin/
│   ├── admin_server.py   # FastAPI admin portal (setup & config web UI)
│   ├── admin_env.py      # .env file reader/writer
│   ├── admin_db.py       # DB-backed config overrides (model pool, system)
│   └── index.html        # Single-page admin UI
├── observers/
│   ├── __init__.py       # Observer registry + discover() + collect_all()
│   ├── base.py           # Observer ABC + safe_observe() + effective_interval
│   ├── time_context/     # Infrastructure: date, time, holidays, silence
│   ├── activity_pattern/ # Infrastructure: 7-day conversation trends
│   └── recent_conversation/ # Infrastructure: last 20 messages
└── skills/
    ├── __init__.py       # Skill registry + auto-discovery + config resolution
    ├── base.py           # Skill ABC + SKILL.md parser + metadata scanner + builders
    ├── memory/           # 8 tools: save, recall, update_core, list, delete, stats, view_core, trash_bin
    ├── oura/             # get_oura_data + co-located observer (health data)
    ├── weather/          # Automation skill + co-located observer (wttr.in)
    ├── habit/            # 3 tools: query, checkin, edit (frequency/pause/snooze) + co-located observer
    ├── reminder/         # manage_reminder (create/list/delete)
    ├── todo/             # manage_todo (add/list/complete/delete)
    ├── diary/            # Automation — status panel auto-refresh (no LLM tools)
    ├── sticker/          # send_sticker, delete_last_sticker
    ├── meal/             # log_meal, query_meals, delete_meal (nutrition tracking)
    ├── web_search/       # web_search (DuckDuckGo, no API key needed)
    └── maintenance/      # Nightly pipeline (archive/dedup/outdated/salience/trash)

data/                     # Runtime data (auto-created)
├── diary.md              # Today's diary entries
└── diary_archive/        # Monthly rollups (YYYY-MM.md)

prompts/                  # Editable prompt templates
├── system_chat/
│   ├── soul.md           # Bot personality / values
│   ├── agent.md          # Agent capabilities (heartbeat, tools, integrations)
│   ├── user.md           # User memory context
│   └── runtime_context.md
├── think_system.md       # Heartbeat decision prompt
├── memory_extract.md     # Memory extraction rules
├── report_morning.md     # Morning briefing (disabled by default)
└── report_evening.md     # Evening reflection (disabled by default)

tests/
├── test_*.py             # Unit tests (pytest)
└── e2e/                  # End-to-end (mock LLM + fake transport)
```

---

## Skill System

Skills are the **only capability unit**. Each skill is a self-contained directory discovered at startup. A skill can have multiple dimensions:

| Dimension | Declaration | Implementation | Orchestrated by |
|-----------|-------------|----------------|-----------------|
| **tool** | `expose_as_tool: true` | `execute()` in handler.py | Engine (tool loop) |
| **observer** | `observer: true` | `observe()` in observer.py | Heartbeat (observe phase) |
| **writes** | `writes: diary: [tags]` | Entries written by skill functions | Diary system |

### Skill Directory Structure

```
mochi/skills/{name}/
├── SKILL.md         # Metadata + tool definitions + config schema (REQUIRED)
├── handler.py       # Skill class with execute() (REQUIRED)
├── __init__.py      # (REQUIRED, can be empty)
├── observer.py      # Co-located observer (OPTIONAL)
└── OBSERVATION.md   # Observer metadata (OPTIONAL)
```

### SKILL.md Format

```markdown
---
name: my_skill
description: What this skill does
expose_as_tool: true
type: tool              # tool | automation | hybrid
tier: chat              # lite | chat | deep (model routing)
multi_turn: false
requires:
  env: [MY_API_KEY]     # auto-disabled if missing
observer: true          # has co-located observer
writes:
  diary: [journal]      # diary files this skill writes to
  db: [my_table]        # DB tables this skill writes to
config:
  diary_journal:
    type: bool
    default: true
    description: "Write events to journal"
sub_skills:
  my_sub: "Sub-skill description"
---

## Tools

### my_tool_name (L0)
What this tool does

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, add) | yes | What to do |

## Usage Rules
- Guidance for the LLM on when/how to use this tool
```

### Tool Risk Levels

Tool headings carry risk annotations: `### tool_name (L0)`

| Level | Meaning | Example |
|-------|---------|---------|
| **L0** | Read-only, safe | `recall_memory`, `get_oura_data` |
| **L1** | Soft-write (internal state) | `save_memory`, `manage_todo` |
| **L2** | External-write (side effects) | Reserved for future use |
| **L3** | Transactional (payment/order) | Reserved for future use |

### Skill Types

- **tool** — LLM-callable tools (memory, reminder, todo, oura, habit, sticker, meal, web_search)
- **automation** — background tasks, not LLM-callable (maintenance, weather, diary)
- **hybrid** — both LLM tools and background triggers (habit is tool + observer)

### Model Tier Routing

Each skill declares its preferred LLM tier: `tier: lite | chat | deep`.

- **lite** — cheap/fast model (classification, simple lookups). Example: sticker
- **chat** — default balanced model. Example: memory, diary, reminder
- **deep** — strongest model (complex reasoning). Example: oura health analysis

When the tool router classifies skills for a message, `resolve_tier()` picks the highest tier needed:

```
User message → classify_skills() → [sticker, memory]
                                  → resolve_tier() → "chat" (memory=chat > sticker=lite)
                                  → get_client_for_tier("chat")
```

Falls back gracefully: tier config empty → uses CHAT_* model. Zero-config by default.

### Metadata Scanner (SSOT)

`scan_skill_metadata()` reads all SKILL.md files **without importing handlers** — safe at module load time. Produces:

- `build_skill_descriptions()` → pre-router skill catalog
- `build_tool_metadata()` → tool name → {skill, risk_level} mapping
- `build_tier_defaults()` → skill → tier for non-default skills

Startup lint validates SKILL.md completeness (warns on missing description, type/tool mismatch, etc.).

### Skill Config

Config resolution priority: **DB override > env var > SKILL.md default**.

Two config declaration formats (front-matter `config:` block preferred):

```yaml
# Front-matter config: block (preferred — supports type casting)
config:
  diary_journal:
    type: bool
    default: true
    description: "Write events to journal"

# Legacy ## Config table (still supported as fallback)
## Config
| Key | Type | Secret | Default | Description |
```

Resolved by `skill_config_resolver.py` with type casting (int/float/bool/str). Skills read via `self.get_config(key)` or pre-resolved `self.config` dict. Admin portal writes trigger `refresh_config()` for hot-reload.

### Skill Toggle

Stored in DB (`skill_config` table, `key='_enabled'`).

**Toggle = all dimensions follow.** When a skill is disabled:
- Its tools are removed from the LLM tool array
- Its co-located observer is skipped in `collect_all()`
- Everything else keeps running. No crash. No error.

### Adding a Skill

1. Create directory with handler.py + SKILL.md + \_\_init\_\_.py
2. Optional: add observer.py + OBSERVATION.md for observer dimension
3. Restart MochiBot — check logs for `✅ Registered skill: {name}`

---

## Observer System

Observers are **read-only, interval-throttled sensors** that feed context into the Heartbeat loop. They never send messages or call skills.

### Two Locations

| Location | Type | Examples |
|----------|------|---------|
| `mochi/skills/{name}/` | **Co-located** — belongs to a skill, toggle linked | oura, weather, habit |
| `mochi/observers/{name}/` | **Infrastructure** — always runs, no toggle | time_context, activity_pattern, recent_conversation |

### OBSERVATION.md Format

```markdown
---
name: my_observer
interval: 30
enabled: true
requires_config: [MY_API_KEY]
skill_name: my_skill    # links toggle to skill (co-located only)
---
```

### Key Behaviours

- **Skill-linked**: co-located observers follow their skill's toggle state
- **Interval override**: `effective_interval` checks DB override first, then OBSERVATION.md default. Adjustable via admin Heartbeat page.
- **Auto-disabled** if `requires_config` env vars are missing at startup
- **Error-isolated**: `safe_observe()` catches exceptions, returns stale cache
- **5 consecutive failures** → auto-disabled for the session
- **Delta detection**: `has_delta(prev, curr)` suppresses noisy Think triggers

---

## Heartbeat Loop

```
Observe (every 20min, 0 LLM calls)
  → Collect soft context: time, silence, user status, todos, reminders
  → collect_all() runs enabled observers (each at own interval)
  → refresh_diary_status() rebuilds Today Status panel (habits ✅/⏳, todos, reminders)
  → Inject diary content into observation

Delta Detection (0 LLM calls)
  → Per-observer has_delta(prev, curr)
  → Think fires only on delta OR 60min fallback

Think (on delta or fallback, 1 LLM call)
  → Receives structured text observation (not raw JSON)
  → Reads Today Status panel for habit progress, timing context
  → Decides: notify (with topic/urgency) | update_diary | nothing
  → ⚡Important habits overdue → MUST notify (topic=habit_nudge, urgency=high)
  → Rate-limited: max N/day, cooldown between messages

Act (execute decision)
  → Send proactive message via transport
  → Or save observation to diary journal
  → Or do nothing (most common)

State Persistence
  → SLEEPING/AWAKE state saved to data/.heartbeat_state
  → Survives restarts (falls back to hour-based heuristic if file corrupted)

Scheduled Tasks
  → Morning / Evening reports (disabled by default)
  → Maintenance pipeline (at MAINTENANCE_HOUR, default 3 AM)
  → Diary archive at maintenance (snapshot → monthly file, clear working file)
```

**Key principle**: Observe is cheap (no LLM), Think is selective, Act is conservative.

---

## Memory

```
Layer 1: Core Memory (~800 tokens, always in system prompt)
    ↑ owned by chat model (add/delete via update_core_memory)
Layer 2: Memory Items (extracted facts — searchable, categorized)
    ↑ extracted by LLM; importance ★1 routine / ★2 important / ★3 critical
Layer 3: Conversation History (raw messages — ephemeral)
```

**Cycle**: Chat → Extract (L3→L2) → Dedup → Outdated Removal → Salience Rebalance → Core Audit

**8 tools** exposed to LLM: save_memory, recall_memory, update_core_memory, list_memories, delete_memory, memory_stats, view_core_memory, memory_trash_bin.

**Soft-delete**: deleted items go to `memory_trash`, kept 30 days, restorable.

### Hybrid Recall

`recall_memory()` uses a 4-phase hybrid search pipeline:

1. **Vec KNN** — sqlite-vec cosine distance on embeddings (if available)
2. **FTS5 BM25** — full-text keyword search with CJK bigram tokenization
3. **Fallback** — importance + recency sort when too few candidates
4. **Scoring** — weighted combination: `vec_sim × W + bm25 × W + (importance + access_bonus) × decay + keyword_boost`

Degrades gracefully: no sqlite-vec → Python cosine fallback → FTS-only → LIKE keyword.

### On-Insert Dedup

`save_memory_item()` checks for duplicates before inserting:

1. **match_hint** — keyword search for status updates (LLM `action: "update"`)
2. **Date-keyed** — `[YYYY-MM-DD]` prefix matching
3. **Text similarity** — normalized SequenceMatcher (≥0.92 general, ≥0.74 same-day)
4. **Vector cosine** — embedding similarity ≥0.92

On match: keeps longer content, bumps importance (MAX), increments `access_count`.

---

## Engine

The processing pipeline from message to response. Currently in `ai_client.py`:

```
1. Route — tool_router classifies intent → select skill subset
2. Resolve tier — infer model tier from classified skills (lite/chat/deep)
3. Build system prompt — Core + Memory + time + usage rules
4. Load conversation history (20 messages)
5. Tool loop (max N rounds):
   a. LLM call with tier-appropriate model + filtered tools
   b. Execute tool calls via skill dispatch
   c. Feed results back to LLM
6. Save response and return
```

**Tool escalation**: if LLM needs a tool mid-turn that wasn't injected, it calls `request_tools` to self-rescue.

**Tier routing**: when `TOOL_ROUTER_ENABLED=true`, the router classifies skills and resolves the model tier. When disabled (default), all calls use the CHAT model.

---

## Diary System

Daily working memory with structured sections, managed by `mochi/diary.py` (L4 infrastructure).

```
data/diary.md              ← Today's file (auto-created, date-rolling)
  ## 今日状態                ← Auto-refreshed from DB each heartbeat tick
    - ⚡Medicine (0/2) (morning and evening) ⏳
    - Exercise (1/1) ✅
    - [ ] Buy groceries
    - 14:00 meeting ⏳
  ## 今日日記                ← Journal entries from Think observations
    - [10:30] User has been active today

data/diary_archive/
  └── YYYY-MM.md           ← Monthly rollups (archived at MAINTENANCE_HOUR)
```

- **DailyFile class**: thread-safe, section-aware, dedup on append, date rolls at MAINTENANCE_HOUR
- **refresh_diary_status()**: queries habits (with progress/importance/context), todos, reminders → rewrites 今日状態
- **Heartbeat integration**: every tick refreshes status → injects into observation → Think reads it
- **Archive**: nightly maintenance snapshots to monthly file, clears working file

## Reminder Timer

Precise time-based reminder delivery via `mochi/reminder_timer.py`.

```
reminder_loop (asyncio task, runs alongside heartbeat)
  → Poll DB for next unfired reminder
  → Sleep until exact remind_at time
  → Fire: send message + mark fired
  → Handle recurrence: daily/weekdays/weekly/monthly/monthly_on:N
```

- Separate from heartbeat — reminders fire at exact times, not on heartbeat interval
- Recurrence: computes next occurrence and reschedules (fired=0, new remind_at)
- Started as asyncio task in main.py alongside heartbeat_loop

---

## Maintenance Pipeline

Nightly housekeeping at `MAINTENANCE_HOUR` (default 3 AM):

1. Diary archive → monthly file, clear working file
2. Dedup → merge near-duplicate memory items (LLM)
3. Outdated removal → LLM identifies stale memories
4. Salience rebalance → promote/demote importance levels
5. Core audit → verify core_memory within token budget
6. Trash purge → hard-delete items older than 30 days
7. Summary → store report for morning briefing

Entire pipeline skippable via `MAINTENANCE_ENABLED=false`.

---

## Admin Portal

Web-based setup and configuration UI, served by FastAPI at `/admin`.

### Pages

| Page | What it manages |
|------|-----------------|
| **Setup** | Status checklist, transport config (Telegram token), integrations overview |
| **Models** | Model registry (add/edit/test/delete), tier assignment (lite/chat/deep) |
| **Heartbeat** | Timing params, proactive limits, report schedules, maintenance toggle + **Observers** (interval tuning) |
| **Skills** | All skills — toggle on/off, config via front-matter `config:` schema, hot-reload |
| **Persona** | Edit persona & prompt templates (soul.md, user.md, etc.) with live hot-reload |

### API Endpoints

**Status & Config:**
| Endpoint | Method | Purpose | Storage |
|----------|--------|---------|---------|
| `/api/status` | GET | System status, config checklist, integration states | Read-only |
| `/api/env` | PUT | Write key-value pairs to `.env` (whitelist enforced) | `.env` file |

**Models:**
| Endpoint | Method | Purpose | Storage |
|----------|--------|---------|---------|
| `/api/models` | GET | List all registered models | DB `model_registry` |
| `/api/models` | POST | Add/update a model | DB `model_registry` |
| `/api/models/{name}` | DELETE | Remove a model | DB `model_registry` |
| `/api/models/{name}/test` | POST | Test model connectivity | — |
| `/api/tiers` | GET | Current tier→model assignments | DB `skill_config._system` + `.env` |
| `/api/tiers/{tier}` | PUT/DELETE | Assign/clear tier override | DB `skill_config._system` |

**Heartbeat:**
| Endpoint | Method | Purpose | Storage |
|----------|--------|---------|---------|
| `/api/heartbeat/config` | GET | All heartbeat params with defaults + overrides | DB `skill_config._system` + `.env` |
| `/api/heartbeat/config` | PUT | Set/clear heartbeat param override | DB `skill_config._system` |
| `/api/heartbeat/state` | GET | Current heartbeat state (awake/sleeping, proactive count) | In-memory |

**Observers:**
| Endpoint | Method | Purpose | Storage |
|----------|--------|---------|---------|
| `/api/observers` | GET | All observers with interval, status, skill linkage | In-memory + DB |
| `/api/observers/{name}/config` | PUT | Override observer interval (1-1440 min) | DB `skill_config._observer:{name}` |

**Skills:**
| Endpoint | Method | Purpose | Storage |
|----------|--------|---------|---------|
| `/api/skills` | GET | All skills with metadata, config status, dimensions | Skill registry + DB |
| `/api/skills/{name}/enabled` | PUT | Toggle skill on/off | DB `skill_config._enabled` |
| `/api/skills/{name}/config` | GET | Read skill config (merged: DB > env > schema default), secrets masked | DB + `.env` + SKILL.md |
| `/api/skills/{name}/config` | PUT | Write skill config values (validated against declaration) | DB `skill_config` + `os.environ` |

**Prompts:**
| Endpoint | Method | Purpose | Storage |
|----------|--------|---------|---------|
| `/api/prompts` | GET | List all editable prompt files with char counts | `prompts/` directory |
| `/api/prompts/{name}` | GET | Read a single prompt file | `prompts/` directory |
| `/api/prompts/{name}` | POST | Save prompt content, hot-reload via `prompt_loader.reload_all()` | `prompts/` directory |

### Storage Model

All admin config uses the same `skill_config` table with namespace prefixes:

| Prefix | Purpose | Example |
|--------|---------|---------|
| `_system` | Heartbeat param overrides | `HEARTBEAT_INTERVAL_MINUTES=15` |
| `_observer:{name}` | Observer config overrides | `interval=60` |
| `{skill_name}` | Per-skill config | `OURA_CLIENT_ID=xxx` |
| `{skill_name}` + `_enabled` | Skill toggle state | `_enabled=false` |

### Auth

Optional: set `ADMIN_TOKEN` in `.env`. When set, all endpoints require `Authorization: Bearer {token}` or `?token={token}` query param.

---

## Fault Isolation

| Layer | Mechanism | On failure |
|-------|-----------|-----------|
| **Skill.run()** | try-except wrapper | Returns `SkillResult(success=False)`, logged |
| **Tool loop** | Engine processes result | Error text fed back to LLM as natural language |
| **Heartbeat** | Outer try-except | Logs error, sleeps, continues next cycle |
| **Observer** | `safe_observe()` | Returns stale cache; 5 failures → auto-disabled |

**Rules:**
- Disabling skill A must never crash skill B
- Cross-skill imports must be inside try-except
- Skills must not propagate exceptions

---

## Shared State

Skills communicate through shared infrastructure only:

```
┌─────────┐  ┌─────────┐  ┌─────────┐
│ Skill A │  │ Skill B │  │ Skill C │
└────┬────┘  └────┬────┘  └────┬────┘
     │            │            │
     ▼            ▼            ▼
  ┌─────────────────────────────────┐
  │  DB (atomic) + Diary (locked)  │
  └─────────────────────────────────┘
```

- DB and diary are the **only shared layers** — never call another skill's `execute()`
- DB operations are atomic (single SQL transactions)
- Diary file protected by `DailyFile._lock` (per-instance threading.Lock)
- Cross-skill imports: read-only functions only, always guarded by try-except

---

## Key Rules

1. **Transport = dumb pipe** — no business logic in transport
2. **Dependency direction** — never import upward (skill → heartbeat = forbidden)
3. **Config, don't hardcode** — thresholds, timings, limits go in .env or DB
4. **Skills are self-contained** — each skill is its own world
5. **Memory is sacred** — don't bypass the 3-layer architecture
6. **Observers are read-only** — never send messages, never call skills
7. **Tool router is additive** — mid-turn escalation can request additional tools
8. **Maintenance is idempotent** — safe to re-run

---

## Testing

```
tests/
├── test_*.py             # Unit tests (pytest)
└── e2e/
    ├── mock_llm.py       # Deterministic LLM stub
    ├── fake_transport.py  # In-memory transport
    └── test_*.py          # E2E suites (chat/heartbeat/reminder/meal/admin)
```

E2E tests boot the full stack with mock LLM + fake transport — no real API calls.
