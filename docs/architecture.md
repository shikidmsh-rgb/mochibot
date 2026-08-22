# MochiBot Architecture

MochiBot is a single-owner companion running as one Python process with SQLite
storage. Its primary design goal is a consistent relationship with minimal
setup, not broad provider or multi-user infrastructure.

## Boundaries

- **Transports** (`mochi/transport/`) adapt Telegram or WeChat messages into
  `IncomingMessage` values and deliver `ChatResult` values. They do not own
  conversation decisions.
- **Main runtime** (`mochi/ai_client.py`) owns user-visible reasoning,
  personality, conversation context, model calls, and tool orchestration.
- **Runtime entries** (`mochi/main_runtime.py`) describe system-owned
  situations, such as bedtime and Weekly curation, that need Main's semantic
  judgment without inventing a user message.
- **Heartbeat** (`mochi/heartbeat.py`) owns sleep gates and independent durable
  clocks for autonomous situations. It creates runtime entries but does not
  interpret observer facts or author companion responses.
- **Skills** (`mochi/skills/`) contain feature behavior and deterministic tool
  operations. Transports and heartbeat call them only through Main or the skill
  registry.
- **Observers** (`mochi/observers/`) are read-only factual producers. Each
  source projects only bounded, provenance-safe unresolved facts; omission on
  a later successful source scan resolves them. Their cached safe views expose
  only allowlisted, bounded fields and can be read without collecting again or
  consuming Attention state.
- **Persistence** (`mochi/db.py`) stores conversation, memory, configuration,
  usage, and tool execution facts.

Dependency direction is transport/heartbeat -> Main -> skills and persistence.
Skills do not import transports or orchestration.

Main 可通过 resident `look_around` 读取 Observer 已有缓存的安全视图。
该工具只读，不触发采集、外部请求或 Attention 状态变化；Free Time 默认
仍不注入生活上下文，只有 Main 主动查看时才获得有界事实。

Main is the semantic judge and author of its actions. The harness gives Main a
lived situation, relevant facts and relationship context, available
capabilities, and the deterministic consequences of using them; it does not
script what Main should say or prescribe a semantic sequence of actions. Hard
commands are reserved for actual safety, protocol, authorization, and data
integrity boundaries. Personality-free Lite work such as classification,
extraction, and validation remains deterministic and may use strict schemas and
output contracts.

## Main conversation flow

1. A transport receives an owner message and creates an `IncomingMessage`.
2. Main loads canonical Core, then the Agent contract, recent conversation, relevant memory, diary,
   and the tools available for that turn.
3. Main calls the configured Main model and executes requested tools through the
   shared policy and execution ledger.
4. The transport delivers the result. The assistant turn is persisted only
   after delivery is confirmed; only then can a complete ordinary conversation
   wake summary and memory extraction.

## Model and provider boundary

The product has exactly two model roles. **Main** owns every personality-bearing
or user-visible situation, including conversation and autonomous runtime
entries. **Lite** is explicitly assigned to personality-free classification,
extraction, and validation. If Lite is not assigned, semantic pre-routing is
skipped rather than borrowing Main and pretending a cheap tier exists.

Built-in chat presets are OpenAI/GPT, DeepSeek, Anthropic/Claude, and Gemini.
DeepSeek and Gemini use their OpenAI-compatible endpoints through the OpenAI
adapter; Anthropic uses its own adapter. A user-supplied HTTPS
OpenAI-compatible API root may use the same OpenAI adapter as an escape hatch;
MochiBot does not add gateway-specific compatibility or guarantee tools,
images, JSON mode, or model parameters beyond what that endpoint implements.
Embedding is optional and off by default. OpenAI, Alibaba Cloud Bailian, and
Azure AI Foundry embedding use the same OpenAI-compatible embedding adapter.

## Tool availability boundary

Each provider round receives one immutable tool-availability snapshot. The
provider schema and dispatch allowlist are derived from that same snapshot, so
a tool cannot execute merely because it exists in the global registry.

Ordinary chat can carry forward up to two recently executed skills marked
`multi_turn`, keeping their routed tools reachable for short natural
follow-ups even when the next message is terse. This only preserves
availability: Main still decides whether to act, and conversation reset,
expiry, disabled/configured state, and the per-turn allowlist remain hard
boundaries. Autonomous runtime entries do not inherit this continuity.

Tool metadata uses `resident`, `routed`, or `on_demand`. Resident tools enter
the turn directly; the Lite pre-router sees only routed skills; and
`request_tools` may add enabled, configured, transport-compatible routed or
on-demand tools for a later provider round. It never authorizes another call in
the same provider response and never mutates the global registry. `locked`
controls only whether the owner may disable a skill. Concrete deny rules, rate
limits, state-change facts, recoverability, and receipts remain execution
contracts rather than an abstract risk taxonomy.

## Self-update flow

Self-update is an owner-requested system skill, not an Observer. Mochi checks
GitHub only when the owner asks to check or update, so heartbeat and
`look_around` do not create background release traffic. Automatic installation
accepts only the latest formal Release from the official repository and only
for a clean local `main` branch; containers, forks, development branches, and
local code changes stay outside this boundary.

The running process never replaces its own code. The update skill prepares an
exact Release only during an ordinary owner chat, and signals the outer
launcher only after the reply is delivered. The launcher then stops Mochi,
fast-forwards to that tag,
syncs dependencies, rolls code back if installation fails, and starts Mochi
again. The restarted transport reports the durable result to the owner.

## Bedtime flow

During night conversations, Main may call the framework-scoped
`enter_bedtime` tool when it understands that the user is genuinely ending the
conversation to sleep. Main leaves a natural farewell in the same tool loop,
or ends the turn quietly when that fits the moment; the transport then claims
and completes the sleep transition. No keyword or separate classifier decides
what the user meant.

Heartbeat-detected silence still creates a `MainRuntimeEntry(kind="bedtime")`
with a lived sleep-transition situation. The heartbeat atomically claims the
transition, Main may use the abilities available in the turn, and the runtime
completes sleep even when model or delivery work fails.

## Nightly and Weekly memory flow

Nightly is deterministic housekeeping. After the configured maintenance hour,
heartbeat claims the logical date in `scheduled_runs`; Diary rollover, Core
size audit, trash retention, and log cleanup run without a
model. A failed claim can retry, while a successful date cannot run twice.

After Monday Nightly succeeds, heartbeat claims the ISO week and creates
`MainRuntimeEntry(kind="weekly_maintenance")`. Weekly runs silently through the
same Main prompt and tool loop, but receives an entry-scoped surface rather than
ordinary chat tools:

- a receipt-backed revision of the free-text Core;
- one atomic curation batch over only the rendered Memory Items and same-user
  evidence messages;
- one atomic relationship curation batch over the visible active user-life graph.

Weekly context contains the previous seven logical days of archived Diary,
recent conversation context, at most 40 new Memory Items, and at most 40
text-related older items. Counts and truncation flags are explicit; unseen rows
are never in scope. Main submits semantic decisions and minimal visible IDs;
the framework binds those decisions to the captured Memory and relationship
snapshots, including content and update-time conflict checks. Memory/Trash/FTS/
vector/KG invalidation commits as one SQLite transaction.
The relationship graph is intentionally limited to people, pets, places, and a
small vocabulary of concrete life relationships. Main may upsert a relationship
only from a visible Memory Item backed by user-message evidence; Core is useful
context but is not evidence. The framework resolves relationship IDs against
the captured active-triple snapshots, and the whole relationship batch commits
or rolls back together.
Weekly's final model text is discarded and no synthetic chat history is stored.
Successful Core patches record a content-hash ISO-week receipt in the canonical
Core store, so a later failure can retry curation without offering the Core
mutation again or retaining an extra copy of Core.

Memory Items are authoritative facts with bounded user-message provenance.
Lite never projects them into the relationship graph. Instead, Weekly Main
reviews the bounded Memory Item package and active graph, using semantic
judgment to keep only durable user-life relationships. Deterministic code
enforces type, predicate, evidence, snapshot, and transaction boundaries.

Conversation context uses a durable per-user rolling summary. Every configured
batch of complete ordinary user/assistant turns is combined with the previous
summary by personality-free Lite, and SQLite advances the cursor only after a
successful result. Until then Main receives the durable previous summary, every
unsummarized complete turn, and the recent role-true window. Context reset starts
a clean summary epoch and rejects any in-flight result from the old epoch.

Memory extraction is another independent Lite coordinator. It consumes fixed
batches of complete eligible normal-chat turns, requires evidence IDs from
same-user messages in that exact batch, optionally embeds candidates before the
transaction, then commits Memory Items and its cursor atomically.
FTS/LIKE is always the text recall path; vectors only add candidates when an
embedding is available, and recent-only rows are never semantic recall filler.

## Self Reminder flow

`manage_reminder(kind="self")` stores a private future intent, not a prewritten
user notification. It may be one-time or recurring. At each scheduled time,
the reminder scheduler claims the row and creates
`MainRuntimeEntry(kind="self_reminder")` containing the intent, scheduled time,
and recurrence. Main sees current Core, Diary, and the capabilities available
on the pinned transport. Recent completed conversation is projected as
bounded read-only evidence rather than replayed as active user/assistant turns.
The complete typed reminder event is the sole provider turn and explicitly
states that no new owner message exists; no synthetic conversation row is
created.

Main may act, prepare a user-visible result, or finish with `[SKIP]`. Tool-only
success and skip require no transport delivery; for a recurring reminder they
advance the same row to its next occurrence. A
deliverable result is serialized before any external send. Text and stickers
are checkpointed independently, so ordinary retry resumes components not yet
checkpointed. A crash after transport success but before its SQLite checkpoint
can duplicate that one component; transport and SQLite provide at-least-once,
not exactly-once, delivery. A stable turn ledger prevents restart from
re-entering Main after any tool attempt, avoiding duplicate side effects at the
cost of conservatively ending an interrupted turn. Each occurrence has a stable turn keyed by its scheduled time. Assistant
history is written idempotently after delivery and marked processed so an
assistant-only system turn does not enter memory extraction.

Ordinary `notify` reminders remain authorized, deterministic deliveries and
may advance the same durable row through a recurrence. Their
voice rendering is prepared once and persisted; transport retry reuses that
outbox. SQLite claims and leases prevent concurrent workers, while the external
send boundary remains at-least-once because transport and SQLite cannot commit
atomically.

Free Time is ephemeral: its text is generated only when the random clock is
claimed, and any transport failure, uncertain delivery, active chat, or recovery
of previously prepared text ends that opportunity without retry. Attention is
fact-driven and may retain a prepared result across retry so tool effects are not
repeated.

## Free Time and Attention flow

Heartbeat keeps two independent clocks. Free Time is randomized, unassigned
companion time; a due Free Time waits until the user's latest message has been
quiet for 30 minutes. Attention runs periodically and can be advanced by a
changed observer fact without moving the Free Time clock, so recent chat never
blocks factual Attention or independent reminder delivery. Sleeping and
long-silence pause gates run before observer or model work.

Both situations enter the standard Main personality and Agent First tool loop.
Free Time receives the last two role-true conversation turns, the rolling
conversation summary, today's Diary journal, and last contact age for lightweight
life continuity. It still excludes the status panel, auto-recall, recent
operations, and semantic routing, so this context remains background rather than
an assigned topic.
Attention receives bounded unresolved facts plus Diary, conversation summary,
temporal context, and role-true recent history. Both start with resident tools
and may request other tools; neither inherits a sticky routed skill.

Observers own factual source state, Main owns meaning/action/expression, and
the transport owns delivery. A Main skip does not resolve observer facts.
Heartbeat stores prepared results before delivery for durable audit. Free Time
never replays that text on another attempt; Attention can reuse it so tool
effects do not repeat after a transport failure. History and proactive delivery
logs are written only after successful text delivery. SQLite cannot commit
atomically with an external transport, so Attention can still duplicate one
component after a crash at that boundary. Daily limits and cooldowns constrain
delivery only; they do not filter topics or decide what facts mean.
