# MochiBot Architecture

MochiBot is a single-user companion running as one Python process with SQLite
storage. Its primary design goal is a consistent relationship with minimal
setup, not broad provider or multi-user infrastructure.

## Boundaries

- **Transports** (`mochi/transport/`) adapt Telegram or WeChat messages into
  `IncomingMessage` values and deliver `ChatResult` values. They do not own
  conversation decisions.
- **Main runtime** (`mochi/ai_client.py`) owns user-visible reasoning,
  personality, conversation context, model calls, and tool orchestration.
- **Runtime entries** (`mochi/main_runtime.py`) describe system-owned
  situations, such as bedtime and Dream curation, that need Main's semantic
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
- **Personal extensions** (`mochi/extensions/`) own reusable guides and code under the Git-ignored
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

Document extensions contain Markdown only. Their manifest supplies discovery
metadata; a shared read-only Skill exposes the activated body through a distinct
tool per guide, without injecting it into capability context or interpreting its
headings as tool schemas. They share authoring, activation, immutable snapshots,
eligibility and usage accounting with Python extensions.

Python extensions are trusted in-process tools, not a sandbox.
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

Main 可通过 resident `look_around` 读取 Observer 已有缓存的安全视图，
不触发采集或外部请求。Free Time 会带入当天适用习惯的已记录进度
（含已完成项目）与今日日记正文；习惯数据由 Skill 从原始记录生成，
遵守技能开关与当前工具资格，不从 Core 猜测完成情况。其他 Observer
事实仍由 Main 主动查看。是否跟进约定、如何表达，由 Main 决定。

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
Main and Dream submit complete Core revisions. The runtime retains the exact
visible document for concurrency checks rather than asking Main to reproduce
patch anchors or old content. Diary uses the same visible-snapshot boundary for
complete journal bodies; deterministic status updates preserve the journal's
formatting. A write without a visible journal returns the current body without
writing, so Main can revise it in a later tool round of the same turn; concurrent
changes still reject stale revisions. Next-day drafts belong to their logical
date and enter that day's journal or archive without replacing existing content.

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

Free Time instead presents the same selected history as completed, read-only
records inside a runtime-labelled activation message, with speaker, timestamp,
and unchanged content. It does not reopen historical user/assistant messages or
replay their reasoning. The activation uses the provider's user input channel,
not owner authority, and is never persisted as a user message or extracted into
memory. Existing execution receipts remain separate from these speech records.

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
plus the last 24 hours of autonomous runtime work, including bedtime, and owner-chat
operations without a confirmed assistant reply. Undelivered chat receipts require
an original user message in the same reset epoch; a model or delivery failure does
not erase completed actions. Silent operations remain visible without inventing
an assistant message, implying delivery, or automatically retrying execution.
These receipts share one count/text budget and respect context resets.
Receipts include read-only, failed and unfinished calls as well as successful
writes; non-success records do not assert the absence of side effects.
They stay inside the same user and reset epoch; Dream keeps its separate
curation context. Tool-provided summaries are data, not instructions or proof that the
user's overall task is complete. Only compact summaries and operation identifiers
are carried, rather than replaying full outputs or arguments. Main retains
judgment about what to do next.

Completed dispatches also retain up to 16,000 characters of their redacted
model-visible text in the same execution ledger, with original execution facts
and explicit retention completeness. The existing short-receipt budget is
unchanged; a receipt ID allows Main to request `read_tool_result` through the
internal-search Skill. Reads return at most 4,000 characters per page, preserve
the original content provenance, and recheck owner and reset boundaries each
time. Reading never re-executes an operation or creates another result snapshot.
An absent snapshot is unavailable, not reconstructed from the summary. These
snapshots share the ledger's lifetime and are independent of Admin evidence;
only the existing credential/media sanitizer is reused, not diagnostic records.

## Human-only runtime evidence

`runtime_trace` records request-time evidence in SQLite for seven days. Each
Main invocation has a trace linked to its existing conversation/run turn ID.
Context preparation records the selected history, document versions, policy and
eligible tools; SDK-boundary spans preserve the actual model, endpoint, request
parameters and response for every attempt, including protocol negotiation.
Tool results and delivery attempts are associated with that trace, while the
existing tool execution ledger remains the authority for executed operations.

Evidence is written before requests begin, not only after successful responses.
Cancellation of a waiter and completion of a synchronous provider request are
distinct facts: a response arriving after cancellation is marked late and does
not reopen the run. Startup marks unfinished work from the previous process as
interrupted. Prepared output is not treated as confirmed delivery.

Admin alone lists and reads this evidence; it is never fed to Main, recall,
summaries or memory extraction. Credential fields and known credential values
are redacted, media bytes are omitted, and oversized payloads are explicitly
marked rather than silently truncated. Trace failures are logged without
changing the agent's execution outcome. Recording adds no model calls and does
not implement automatic alerting, replay or recovery.

Admin derives execution diagnostics from compact terminal facts alongside the
original trace and tool ledger. Model retries share an explicit request identity;
tool recovery requires identical unredacted arguments or a framework-owned
document target. Unrelated successes and late responses cannot clear failures,
and unknown side effects remain unknown after subsequent successes. Delivery,
model completion, tool results and reported data changes stay separate.
Truncation is distinct from exceeding a verified provider's output budget.
Missing historical evidence is not reconstructed as success. These diagnostics
neither assess Main's initiative nor change its tools, prompt or retry behavior.

Pre-model preparation has its own spans for model-client setup, Core, history,
recall, routing, habits, Diary and tool/prompt assembly, including failed or
cancelled waits. `scripts/diagnose.py` reads existing traces by time, runtime kind,
diagnostic state or exact trace ID without constructing a model client. It can
export the recorded evidence and an explicitly selected systemd journal window;
missing logs are reported, and the export never overwrites an existing file.
This developer path does not reconstruct prompts or replay user actions.

SDK attempts also retain human-only [token distribution](token-distribution.md)
facts: original input sizes, explicit local reference-token counts and the
provider's separate actual usage. Prompt assembly supplies source ranges without
changing model-visible text. Counts are captured before redaction, while media,
encrypted reasoning and provider-internal overhead remain unallocated.
Read-only queries use the existing evidence store and retention; these facts
never enter Main's context or influence its decisions.

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
Verified GPT-5.6 Sol, GPT-6 Sol and Astra models use the Responses API to keep
reasoning and function tools available together; their tool rounds replay provider
output and paired function results without relying on provider-stored conversation state.
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
Free Time excludes prior-turn reasoning from its completed-history records;
reasoning returned during its current tool loop still accompanies that loop.
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
prompt lists unloaded requestable tool names grouped by skill. An exact tool
request loads only that tool; skill names and query matches load the skill's
requestable tools. Mixed requests deduplicate tools and distinguish new additions
from tools already available. A skill's shared capability
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

The resident `manage_settings` is Main's single configuration surface for
runtime preferences, skill switches and parameters, personal development,
adaptive tool loading, and read-only model summaries. The shared settings
service supplies the live catalog and mutation rules to Main and overlapping
Admin endpoints; existing stores remain authoritative. Reads do not start model
clients, contact providers, or execute inactive extensions. Secret fields expose
only configured state. Model summaries distinguish saved and loaded configuration.
Transport-authenticated user status authorizes requested changes in chat;
Main may inspect settings and manage adaptive loading autonomously.
Heartbeat resolves sleep/wake values at each decision boundary. Message wake
eligibility and scheduled fallback wake are separate preferences. Core may
remember a preference but is never runtime configuration authority.

Only explicitly adaptive tools may move between `on_demand` and `routed`
loading, starting at their declared tier. Document readers default to `routed`.
Nightly derives that projection from successful,
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
conversation to sleep. Its result brings the same day review used by
heartbeat bedtime into the existing tool loop, without starting another Main
turn. Main can act and either speak or finish silently, then the transport
claims and completes the sleep transition. No keyword or
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
Both entries review original user and confirmed assistant messages from the
Diary's logical day, including after-midnight conversation and standalone
deliveries. The current context-reset boundary still applies. The bounded
review reports its time range, message counts and truncation explicitly; a
rolling summary is not presented as the day's original conversation. Heartbeat
bedtime replaces the ordinary recent-history window with this review rather
than repeating it. Today's journal and any tomorrow draft accompany the review,
with an empty journal distinguished from one not loaded for this entry.
Main chooses what to retain or do; writing a diary is not a completion gate.
Tool outcomes survive a silent finish, independently of transport delivery.
`[SKIP]` suppresses speech, not already completed internal work, and does not
trigger another model call to manufacture a farewell.

## Nightly and Dream memory flow

Nightly is deterministic housekeeping. After the configured maintenance hour,
heartbeat claims the logical date in `scheduled_runs`; Diary rollover, Core
size audit, trash retention, and log cleanup run without a
model. A failed claim can retry, while a successful date cannot run twice.

After Nightly succeeds, heartbeat checks accumulated unreviewed material locally.
Enough material, elapsed time with pending material, or a pending continuation
can create `MainRuntimeEntry(kind="dream")`, at most once per logical day.
Dream replaces Weekly; it uses the same Main prompt and tool loop silently,
with an entry-scoped surface rather than ordinary chat tools:

- a receipt-backed complete revision of the visible free-text Core, with snapshots;
- one atomic curation batch over only the rendered Memory Items and same-user
  evidence messages;
- one atomic relationship curation batch over the active user-life graph.
- management of visible, unstarted self reminders through the existing reminder
  store and scheduler, never ordinary notify reminders.

Dream progresses through Memory content/evidence versions and archived personal
Diary fragments rather than a moving weekly window. Separate durable progress
keeps budget-truncated tails pending. Its own Memory writes acknowledge their
versions in the same transaction and do not become new trigger material; later
external edits or evidence additions do. Older records are initially baselined,
not claimed to have been reviewed. Original messages, memory, Diary and Core
remain authoritative; batch snapshots and progress are processing state.

Context supplies bounded new and related memory, source-message excerpts,
Diary, relationships, self reminders and prior operation receipts. It does not
duplicate ordinary conversation history, route with Lite or automatically recall.
Counts and truncation flags are explicit; unseen rows are never in scope.
Core capacity is reported against Main's exact visible document, and Memory
distinguishes stored source IDs from currently readable evidence. The writing
model owns semantic atomicity; punctuation is not a fact classifier.
Memory edits compare content, update time and evidence versions, and
Memory/Trash/FTS/vector/KG invalidation commits as one SQLite transaction.
Main submits the intended changes and visible IDs; the framework retains the
exact Memory and relationship snapshots instead of asking Main to transcribe
content, timestamps, or whole triples. Successful Memory curation refreshes the
visible relationship evidence for subsequent model rounds. Relationship curation
does not require a no-op Memory call first.
The relationship graph is intentionally limited to people, pets, places, and a
small vocabulary of concrete life relationships. Main may upsert a relationship
only from an exact visible Memory Item snapshot backed by user-message evidence;
Core is useful context but is not evidence. Archives use exact active-triple
snapshots, and the whole relationship batch commits or rolls back together.
Dream's final model text is discarded and no synthetic chat history is stored.
Successful operations retain batch-scoped receipts, with SQLite mutations and
their receipts committed together. Core retains its existing file/snapshot and
content-hash receipt boundary. Later attempts see current state and prior results
without repeating committed operations. No cross-store global transaction is
claimed. Only normal, complete model termination without unresolved execution
failure advances material progress; a no-change decision can complete normally.
Active owner chat takes priority between calls, preserving already committed
effects and leaving unfinished work for a later daily opportunity.
The [Dream contract](dream.md) defines counting, limits and user-visible controls.

Memory Items are authoritative facts with bounded user-message provenance.
Lite never projects them into the relationship graph. Instead, Dream Main
reviews the bounded Memory Item package and active graph, using semantic
judgment to keep only durable user-life relationships. Deterministic code
enforces type, predicate, evidence, snapshot, and transaction boundaries.

Conversation context uses a durable per-user rolling summary. Every configured
batch of complete ordinary user/assistant turns is combined with the previous
summary by personality-free Lite, and SQLite advances the cursor only after a
successful result. Until then Main receives the durable previous summary, every
unsummarized complete turn, and the recent role-true window. Context reset starts
a clean summary epoch and rejects any in-flight result from the old epoch.
On the official DeepSeek adapter, both summary generation and its bounded
compression retry disable thinking per request, without changing shared model
defaults, Main, Dream, or other Lite tasks.

Memory extraction is another independent Lite coordinator. It consumes fixed
batches of complete eligible normal-chat turns, requires evidence IDs from
same-user messages in that exact batch, optionally embeds candidates before the
transaction, then commits Memory Items and its cursor atomically.
Only a complete, validated model result may enter that transaction; output
truncation leaves the batch pending rather than accepting a partial extraction.
The official DeepSeek adapter supports a per-request thinking override; this
coordinator disables thinking for its extraction request without changing the
shared client's defaults, Main, or other Lite tasks.
FTS/LIKE is always the text recall path; vectors only add candidates when an
embedding is available, and recent-only rows are never semantic recall filler.

## Self Reminder flow

The resident `schedule_self_reminder` and routed `manage_reminder(kind="self")`
store a private future intent, not a prewritten user notification.
At the scheduled time, the reminder scheduler claims the
row and creates `MainRuntimeEntry(kind="self_reminder")`; Main sees current
Core, conversation, Diary, and the capabilities available on the pinned
transport, without a synthetic user message.

Self Reminder turns run one at a time, through delivery confirmation, so the
next turn reads the preceding turn's recorded work and delivered speech.
Waiting does not claim the reminder or extend its original expiry; cancellation
and schedule changes remain effective until its turn begins. Ordinary `notify`
reminders do not wait for Self Reminder turns.

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

Heartbeat persists a bounded random plan after waking, within the configured
awake hours. Opportunities are drawn only from the remaining time up to 21:00
or the configured rest hour, whichever is earlier. Sleep does not create a
plan; restarting or waking again does not redraw an existing same-day plan.
Setting changes continue to share the day's already-consumed budget. The configured
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
