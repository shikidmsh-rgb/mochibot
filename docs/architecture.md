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
- **Heartbeat** (`mochi/heartbeat.py`) owns sleep gates and the durable daily
  Free Time plan. It creates runtime entries but does not interpret observer
  facts or author companion responses.
- **Skills** (`mochi/skills/`) contain feature behavior and deterministic tool
  operations. Transports and heartbeat call them only through Main or the skill
  registry.
- **Personal workspace** (`mochi/personal_workspace.py`) is Main's single
  document/source authoring surface, delegating to Markdown and extension stores
  rather than exposing the host filesystem. Editing, execution and live
  activation are separate operations chosen by Main.
  The existing `workspace` skill continues to own Diary only.
- **Personal extensions** (`mochi/extensions/`) own code under the Git-ignored
  `data/extensions/` directory, outside official source. The Agent document
  points Main to the default-enabled workspace; tools and the on-demand guide
  supply details. Disabling development blocks source mutation, execution and
  activation, not documents, inspection or installed tools.
- **Observers** (`mochi/observers/`) are read-only factual producers. Their
  cached safe views expose only allowlisted, bounded fields and can be read
  without collecting again. Observations do not independently wake Main.
  Disabling Free Time stops autonomous opportunities, not awake-state cache
  refresh or Diary status maintenance.
- **Persistence** (`mochi/db.py`) stores conversation, memory, configuration,
  usage, and tool execution facts.
- **Document storage** (`mochi/mochi_files_store.py`) retains Main's private
  Markdown space under `data/mochi_files/`, accessed through the personal
  workspace. Main alone decides whether, what, and how to write.

Dependency direction is transport/heartbeat -> Main -> skills and persistence.
Skills do not import transports or orchestration.

Personal extensions are trusted in-process Python tools, not a sandbox.
`mochi.mod_api.v1` exposes the existing Skill context/result, injected
configuration, extension-owned persistent data and the candidate helper.
Development scripts use bounded subprocesses and disposable copies, not another
Main runtime. Activation validates an immutable candidate before replacing the
live registry; in-flight calls retain their code. Failed activation preserves
the old registry, not necessarily data or external effects. Main judges the
work; no verification-success gate or automatic continuation loop does so.
The [extension guide](extensions.md) owns authoring, compatibility, storage and
recovery details, including legacy support and unsupported integration hooks.

Personal workspace files remain separate from Core, Memory, KG, Diary, and
SQLite. Workspace tools are available only to Main tool calls, not Lite,
Nightly, scripts, or harness jobs. Browse results are current-turn agent-authored
artifacts, not external truth or automatically recalled context.
The registry retains complete tool ownership separately from current eligibility;
changing the development setting can hide execution tools without losing their
registration.

Main 可通过 resident `look_around` 读取 Observer 已有缓存的安全视图。
该工具只读，不触发采集或外部请求；Free Time 默认
仍不注入生活上下文，只有 Main 主动查看时才获得有界事实。

Main is the semantic judge and author of its actions. The harness gives Main a
lived situation, relevant facts and relationship context, available
capabilities, and the deterministic consequences of using them; it does not
script what Main should say or prescribe a semantic sequence of actions. Hard
commands are reserved for actual safety, protocol, authorization, and data
integrity boundaries. Personality-free Lite work such as classification,
extraction, and validation remains deterministic and may use strict schemas and
output contracts.

Core has one source, `data/core.md`. A missing file starts with a short,
open-ended seed; an existing file is preserved. Startup never imports identity
templates or a database Core. Ordinary document edits retain their snapshots.
Main and Weekly submit complete Core revisions. The runtime retains the exact
visible document for concurrency checks rather than asking Main to reproduce
patch anchors or old content. Diary uses the same visible-snapshot boundary for
complete journal bodies; deterministic status updates preserve the journal's
formatting. Next-day drafts belong to their logical date and enter that day's
journal or archive without replacing existing content.

## Main conversation flow

1. A transport receives an owner message and creates an `IncomingMessage`.
2. Main loads canonical Core, then the Agent contract, recent conversation, relevant memory, diary,
   and the tools available for that turn.
3. Main calls the configured Main model and executes requested tools through the
   shared policy and execution ledger.
4. The transport delivers the result. The assistant turn is persisted only
   after delivery is confirmed; only then can a complete ordinary conversation
   wake summary and memory extraction.

Main's recent conversation includes bounded, timestamped standalone assistant
messages only after confirmed delivery, alongside complete ordinary turns.
These messages do not become user turns or enter Lite summary/extraction batches.
User isolation and conversation resets apply to both kinds of history.
Absolute message times are supplied as a separate, ordered context table.
User and assistant message bodies remain unchanged; framework metadata is never
prepended to speech, where it could become a self-reinforcing reply pattern.

For owner message turns, the system prompt holds only cross-turn stable content
(Core, Agent contract, capability guides). Per-turn facts — the history time
table, today, summary, recent operations, recalled memory, habit snapshot and
current time — travel as one read-only `turn_context` message just before the
current input and are never persisted, so providers can reuse the system +
history prefix across turns. Runtime entries keep that context in system.

Once per logical day, during awake hours, the first owner chat turn or Free
Time run — whichever comes first — also carries a "new day" look at the last
seven archived diaries (newest days kept within budget). It is marked done in
`scheduled_runs` only after that Main turn completes, so a restart does not
repeat it and a failed turn retries. When the configured offset is UTC+8, the
current time line also lists mainland holidays and make-up workdays in the next
two weeks.

Automatic recall searches the current sentence and a bounded recent completed
conversation independently, prioritizing the current topic and deduplicating
Memory Items. Embedding cache misses share one batch request; there is no extra
Lite selection call. Repeat cooldown applies only to the same query context
after Main has received it. Reference counts describe actual Main exposure, not
unshown search candidates.

Explicit personal-history search uses local conversation, Diary journal bodies,
and text-only Memory recall. Unlike automatic context, an explicit search may
cross a conversation reset; it excludes the current turn and does not change
reset boundaries. Querying does not count as Memory exposure: returned IDs are
counted only after a subsequent successful Main call has received the results.

The existing execution ledger supplies bounded receipts for the completed turns
visible in Main's conversation context, independent of message wording or routing,
plus the last 24 hours of autonomous runtime work. Silent
operations remain visible without inventing an assistant message or implying
delivery. These receipts share one count/text budget and respect context resets.
Receipts include read-only, failed and unfinished calls as well as successful
writes; non-success records do not assert the absence of side effects.
They stay inside the same user and reset epoch; Weekly keeps its separate
curation context. Tool-provided summaries are data, not instructions or proof that the
user's overall task is complete. Only compact summaries and operation identifiers
are carried, rather than replaying full outputs or arguments. Main retains
judgment about what to do next.

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
GPT-6 Sol uses the Responses API to keep reasoning and function tools available
together; its tool rounds replay provider output and paired function results
without relying on provider-stored conversation state.
Embedding is optional and off by default. OpenAI, Alibaba Cloud Bailian, and
Azure AI Foundry embedding use the same OpenAI-compatible embedding adapter.

Optional image generation is a separate framework capability, not a third
conversational role. `image_service` stores one encrypted configuration, resolves
the image API from known official endpoints or an explicit protocol choice, and
returns in-memory images. OpenAI Images and Gemini native `generateContent`
adapters receive only the explicitly supplied description. Admin manages
configuration only, with no image generation, upload, preview, or send endpoints.
Configuration changes never probe models or trigger generation; a generation
request is not retried automatically.

WeChat image delivery is an independent transport capability. It encrypts and
uploads bytes before sending the image message through the same reply-context
and failure boundary as text. An upload is not a delivered message. When configured
and enabled, a routed image-generation skill lets Main generate and send an image
in an owner-authorized WeChat chat turn. The image is shown to Main in the same
turn after delivery, but its bytes are never stored in conversation history.
Other runtime entries and Telegram cannot use this skill. There is no durable
image gallery or autonomous image trigger.

Provider-returned reasoning is protocol metadata, not conversation content or
memory evidence. Delivered assistant records and durable delivery outboxes
preserve it separately from visible text. Main replays it only for the same
endpoint and model, alongside messages already selected by its context policy;
it never enters summaries, memory extraction, or user-facing message projections.
Older, non-model, or different-provider messages have no reusable reasoning, so
a provider that requires the field may still receive an empty compatibility
placeholder for those messages. Matching reasoning, including an explicitly
empty value, is never replaced by that fallback.

## Tool availability boundary

Each provider round receives one immutable tool-availability snapshot. The
provider schema and dispatch allowlist are derived from that same snapshot, so
a tool cannot execute merely because it exists in the global registry. Main
dispatches only provider-completed tool-call rounds whose arguments parse and
match that snapshot's schema; rejected calls receive paired, turn-local tool
errors so the model can recover without recording or executing them. Every
paired result states whether it succeeded; failures also state whether the
skill handler started and include retry and durable-state facts when known.
Successful external Web results also carry source and authority facts so Main
can distinguish untrusted data from user or system instructions. These facts
come from execution contracts, never by interpreting result prose.

Tool metadata uses `resident`, `routed`, or `on_demand`. Resident tools enter
the turn directly; the Lite pre-router sees only routed skills; and
`request_tools` may add enabled, configured, transport-compatible routed or
on-demand tools for a later provider round, including newly activated personal
resident tools that were absent at turn startup. It never authorizes another call in
the same provider response and never mutates the global registry. The system
prompt lists unloaded requestable skills by name only; a skill's capability
context arrives with the `request_tools` result that loads it. `locked`
controls only whether the owner may disable a skill. Concrete deny rules, rate
limits, state-change facts, recoverability, and receipts remain execution
contracts rather than an abstract risk taxonomy.

The Lite pre-router is a bounded optimization: after a short timeout Main
answers without routed tools and can still use `request_tools`. Ordinary owner
chat keeps an in-memory session toolbox: non-resident tools visible in the
previous turn, including those loaded by `request_tools`, stay loaded while
messages continue within the idle window. The order is resident, carried, then
newly routed, so follow-ups keep their tools and the provider schema stays
stable. Every turn revalidates carried tools against the live registry and
policy; idle expiry, a conversation reset, restart, or an oversized toolbox
starts over. Runtime entries never carry chat tools.

After live extension activation, in-flight calls finish on their existing
immutable code snapshot. Subsequent provider rounds refresh tools already
authorized for the turn; newly added names still require `request_tools`.
Authoring, activation, and use can therefore complete within one conversation
turn. Shared call budgets bound resource use without prescribing a workflow;
explicit configured limits remain effective. Defaults belong in configuration
and the extension guide rather than this architecture overview.

Explicit owner requests to change sleep/wake hours, timezone, or the daily
Free Time limit route `manage_agent_settings` into Main's turn.
Transport-authenticated owner status is carried into tool dispatch before the
tool writes the existing system-override store. Heartbeat resolves sleep/wake
values at each decision boundary. Core may remember a preference but is never
runtime configuration authority.

Only explicitly adaptive tools may move between declared `on_demand` and
effective `routed` loading. Nightly derives that projection from successful,
distinct ordinary chat turns; autonomous work does not count. Main can pin or
reset eligible tool visibility. This never grants resident loading, bypasses
eligibility, or expands an in-flight round's allowlist. Definitions remain the
source of declared contracts; SQLite stores only the current loading projection.

## Stable-release updates

Admin and the system-update skill share one framework update service. Main
interprets the owner's update request; code enforces owner-chat authorization,
the fixed official stable-release source, a clean Git worktree, fast-forward
history, and untouched configuration/data paths. Preparation pins one exact
commit and is not an installation success.

Only confirmation of the complete final reply, or completion of the Admin HTTP
response, hands a prepared update to the launcher. Exit 44 runs a fresh updater
process; ordinary restart 42 never installs a pending request. The updater
records real complete or partial outcomes, without resetting local changes.
After restart, the result is acknowledged only after successful delivery.
Direct Main/systemd and container starts can check releases but cannot install
through this launcher-only path.

## Bedtime flow

During conversations, Main may call the framework-scoped
`enter_bedtime` tool when it understands that the user is genuinely ending the
conversation to sleep. Main leaves a natural farewell in the same tool loop,
then the transport claims and completes the sleep transition. No keyword or
separate classifier decides what the user meant, and the tool is not restricted
to scheduled rest hours. Sleep pauses Free Time until an eligible owner message
or the next fallback wake time after sleep began. The existing persisted state
timestamp preserves this boundary across restarts.

Heartbeat-detected silence still creates a `MainRuntimeEntry(kind="bedtime")`
with a lived sleep-transition situation. The heartbeat atomically claims the
transition, Main may use the abilities available in the turn, and the runtime
completes sleep even when model or delivery work fails. Bedtime uses the shared
Main preparation and delivery callbacks: its model timeout ends before transport
delivery begins, leaving delivery its own timeout and confirmation boundary.
Unconfirmed delivery is recorded without adding a delivered message or retrying.
When the recent conversation already completed a bedtime farewell, Main may choose `[SKIP]`
and let the transition finish without sending a duplicate goodbye.

## Nightly and Weekly memory flow

Nightly is deterministic housekeeping. After the configured maintenance hour,
heartbeat claims the logical date in `scheduled_runs`; Diary rollover, Core
size audit, trash retention, and log cleanup run without a
model. A failed claim can retry, while a successful date cannot run twice.

After Monday Nightly succeeds, heartbeat claims the ISO week and creates
`MainRuntimeEntry(kind="weekly_maintenance")`. Weekly runs silently through the
same Main prompt and tool loop, but receives an entry-scoped surface rather than
ordinary chat tools:

- a receipt-backed complete revision of the visible free-text Core, with snapshots;
- one atomic curation batch over only the rendered Memory Items and same-user
  evidence messages;
- one atomic relationship curation batch over the active user-life graph.

Weekly context contains the previous seven logical days of archived Diary,
recent conversation context, at most 40 new Memory Items, and at most 40
text-related older items. Counts and truncation flags are explicit; unseen rows
are never in scope. Memory edits compare both content and update time, and
Memory/Trash/FTS/vector/KG invalidation commits as one SQLite transaction.
Main submits the intended changes and visible IDs; the framework retains the
exact Memory and relationship snapshots instead of asking Main to transcribe
content, timestamps, or whole triples. Successful Memory curation refreshes the
visible relationship evidence before relationship changes are accepted.
The relationship graph is intentionally limited to people, pets, places, and a
small vocabulary of concrete life relationships. Main may upsert a relationship
only from an exact visible Memory Item snapshot backed by user-message evidence;
Core is useful context but is not evidence. Archives use exact active-triple
snapshots, and the whole relationship batch commits or rolls back together.
Weekly's final model text is discarded and no synthetic chat history is stored.
Successful Core revisions record a content-hash ISO-week receipt in the canonical
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

The resident `schedule_self_reminder` and routed `manage_reminder(kind="self")`
store a private future intent, not a prewritten user notification.
At the scheduled time, the reminder scheduler claims the
row and creates `MainRuntimeEntry(kind="self_reminder")`; Main sees current
Core, conversation, Diary, and the capabilities available on the pinned
transport, without a synthetic user message.

Main may act, prepare a user-visible result, or finish with `[SKIP]`. Tool-only
success and skip are terminal outcomes that require no transport delivery. A
deliverable result is serialized before any external send. Text and stickers
are checkpointed independently, so ordinary retry resumes components not yet
checkpointed. A crash after transport success but before its SQLite checkpoint
can duplicate that one component; transport and SQLite provide at-least-once,
not exactly-once, delivery. A stable turn ledger prevents restart from
re-entering Main after any tool attempt, avoiding duplicate side effects at the
cost of conservatively ending an interrupted turn. Assistant
history is written idempotently after delivery and marked processed so an
assistant-only system turn does not enter memory extraction.

Ordinary `notify` reminders remain authorized, deterministic deliveries. Their
stored message, prefixed with `⏰ `, is prepared without any model call and
persisted; transport retry reuses that outbox. SQLite claims and leases prevent
concurrent workers, while the external
send boundary remains at-least-once because transport and SQLite cannot commit
atomically.

Both reminder kinds expire five minutes after their scheduled time. Startup,
claims, preparation timeouts, retries, and each outgoing transport chunk honor
that original deadline. Expired reminders retain their prepared content and
execution evidence but cannot re-enter Main or send, even after restart or
transport recovery. A request issued before expiry can still finish afterward;
its receipt is recorded, but no further chunk may start. Already completed tool
effects are not undone. Recurring reminders retain the expired occurrence and
schedule only the next unexpired occurrence, without replaying missed dates.
Updating an occurrence is allowed only before processing starts; it cannot
erase already performed tool work or prepared delivery evidence.

Autonomous Free Time delivery is single-attempt: unavailable transport,
explicit rejection, and uncertain delivery are terminal, separately audited
outcomes. No prepared text or tool loop is replayed on a later heartbeat or
restart. An abandoned turn expires, retaining its result and execution evidence.
The next opportunity creates a new present-time situation, not a retry of an old one.
Explicit reminders retain independent durable retries within their five-minute
delivery window.

WeChat persists the owner's latest reply context using the existing encrypted
configuration storage, scoped to the bot credentials, endpoint, and recipient.
Restart restores that context; a session rejection invalidates the matching
token without erasing a newer inbound token. Missing context is a local
unavailable state, not a blind API attempt. Send diagnostics retain numeric
HTTP/API error codes, never reply tokens or raw response bodies.

WeChat sends each runtime-initiated text result (Free Time, reminders,
and silent bedtime) as one message, preserving paragraph breaks and converting
bubble delimiters to paragraph breaks. Only the transport length limit splits
that text, and each chunk still requires delivery authorization. Ordinary chat
keeps conversational bubbles; other transports retain their existing formatting.

## Free Time flow

Heartbeat persists a bounded random plan for each local day. The configured
limit bounds Free Time opportunities, not a quota of messages. Missed,
sleep-conflicting and active-chat-conflicting opportunities expire rather than
being queued for later. Sleeping and long-silence pause gates run before
observer or model work.

Free Time enters the standard Main personality and Agent First tool loop.
Free Time receives the last two role-true conversation turns, up to five recent
standalone deliveries, and bounded execution receipts for continuity. Standalone
history starts with the selected conversation window, or uses the latest five
deliveries since reset when there are no complete turns; midnight does not clear
it. Free Time deliberately excludes Agenda, Diary, summaries, auto-recall, and semantic routing,
so recent conversation remains background rather than an assigned topic; the
one exception is the daily day-start diary look when Free Time comes first.
It starts with resident tools and may request other tools; it does not inherit
a sticky routed skill.

Observers own factual source state, Main owns meaning/action/expression, and
the transport owns delivery. Main can inspect cached facts through `look_around`.
Heartbeat stores the prepared result before delivery for audit, not as a retry
outbox. Only currently due opportunities can run. An incoming owner
conversation invalidates an earlier Free Time turn even if that conversation
finishes before the model returns. The active-chat boundary includes reply
delivery; it does not undo effects already completed. Each unsent bubble/chunk
requires the current lease, awake situation, and unchanged chat generation.
Ordinary replies do not inherit this gate, and reminders use their own deadline
rather than the awake-state gate.
History and proactive delivery logs are written only after confirmed text
delivery, even if a later sticker fails. SQLite cannot commit atomically with an
external transport: a crash can leave delivery uncertain, but does not authorize
replay. The scheduler bounds opportunities without filtering topics or deciding
what facts mean.
