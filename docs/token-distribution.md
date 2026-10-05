# Token distribution evidence

Token accounting belongs to human-only `runtime_trace` evidence. It does not
change prompts, tool eligibility, context budgets, or agent decisions. The
read-only CLI below adds no UI, HTTP route, bot command, or model request.

## What is recorded

Each SDK attempt records `facts.token_distribution` before sending the request.
It contains the actual requested model and a list of input parts, each with:

- `path`: the location in the SDK request, including message/tool indexes.
- `source`: assembly ownership where known (Core, Agent, capability context,
  requestable tools, diary context, recall, summary, operations, time and runtime
  entry sections), historical messages and current input; otherwise the message
  role. Repeated identical messages retain their occurrence ownership.
  Tool schemas and calls are
  attributed by tool name. Results use that name when their paired call is in
  the request, otherwise the call ID.
- `representation`: `text`, `canonical_json`, or `opaque`.
- `chars` and `utf8_bytes`: exact sizes of that representation before redaction.
- `reference_tokens`: local `tiktoken` / `o200k_base` count, or `null` for opaque
  inputs. Text parts also retain original character offsets.

Offsets refer to the original text, not a possibly shorter redacted export.

Every text field is encoded as a whole. A token crossing a source boundary is
assigned to the source containing its first byte and counted in
`cross_boundary_tokens`. Subdividing the text therefore cannot inflate the
total. Separators are represented explicitly. Literal special-token strings
are encoded as ordinary text, not interpreted as control tokens.

Tool schemas, tool calls and output formats are counted using sorted, compact
JSON with literal Unicode. This is a reproducible reference representation,
**not** the provider's internal tool serialization. Image/audio/file blocks,
encrypted reasoning and unclassified blocks have sizes but no invented token
count. `totals.uncounted_parts` makes this visible. Message role framing and
other provider-internal overhead are not counted.

`provider_usage` preserves the usage object reported for that exact attempt.
Input/output, cache and reasoning counters remain separate fields in their
native provider convention; nested counters must not be added to their parent
totals. Missing usage remains `null`, never zero. Failed requests, retries and
late responses keep their own attempt records and statuses. An `operation_id`
connects attempts belonging to one logical model round.

The reference encoding is deliberately explicit and is **not claimed to be
the configured model's tokenizer**. There is no proportional allocation of
provider usage to local parts, nor a claim that a discrepancy is all protocol
overhead. Tokenizer differences, media and hidden provider processing prevent
that conclusion.

## Read-only access

For a complete interval, run:

```powershell
python scripts\token_report.py --days 1
python scripts\token_report.py --days 7 --exclude-evaluations --json
python scripts\token_report.py --since 2026-10-05T00:00:00+08:00 --until 2026-10-06T00:00:00+08:00 --role MAIN --stage initial --calls --json
```

The default is the last 24 hours, not the current calendar day. Intervals are
left-closed and right-open. Optional `--user-id`, `--kind`, `--model`, `--role`
and `--stage` filters are exact matches. `--calls --json` includes per-request
metadata but never prompt, response or tool-result bodies.

This reader consumes every matching SDK attempt in a single read-only database
snapshot, not just one API page. It groups activity, model, role, stage and
recorded turn, with explicit evaluation and anomaly subtotals. Additional
attempts include repeated operation IDs within the selected trace/window and
explicit summary compression retries; this is not a reconstruction of attempts
outside the interval. Recorded run states are not claims that a semantic task
was completed.

Calls marked `evaluation` / `cache_evaluation`, or linked to a usage row with
`call_type=evaluation`, are separated. Old traffic without such labels is not
guessed to be evaluation. Missing old role, stage and fingerprint metadata is
unknown. Original per-attempt usage can still be read from old evidence, but
prices and fingerprints are not reconstructed from redacted text.

`runtime_trace` is the per-attempt authority. Completed LLM results carry an
internal span ID into `usage_log`; repeated logging of that same result cannot
create another ledger row. The ledger keeps its frozen billing snapshot even
after detailed evidence expires. The report does **not** add ledger totals to
trace totals. Unlinked historical ledger rows are counted separately across the
instance because those older rows have no reliable owner/attempt identity.
The seven-day detailed-evidence retention still applies; an older requested
start is explicitly partial coverage. Text/embedding SDK calls are covered,
not a complete invoice for image, speech or unrelated external services.

Use `mochi.runtime_trace.query_token_distribution(user_id, ...)` from developer
Python code. Optional filters are `trace_id`, timezone-bearing `since` and
`until`, and runtime `kind`. `limit` is 1–100 (default 30); pass the returned
`next_before` as `before` to continue paging.

The result contains `calls`, `summary` and `retention_days`. Each call includes
its trace/span IDs, operation ID, status, start time and distribution.
`summary.by_source` sums attempted input **only for the returned page**, including
repeated input in retries and tool continuations. It is not billed usage or a
whole-window total. Actual usage remains available per attempt.

The reader opens the existing database read-only and checks the owner filter.
It does not initialize schemas, load a model/tokenizer, or rebuild context.
Existing trace detail/export access also includes the recorded facts.
Records follow the existing seven-day retention. Older requests without these
counts are explicitly unavailable; redacted historical text is never presented
as an exact reconstruction of the original input.

## Optional request-time pricing

No tariffs are guessed from a model name, and no public list price is assumed
to be the price of a gateway. To enable estimates, create the Git-ignored
`token-prices.json` beside the configured database. Copy the exact `provider`,
`endpoint` and requested `model` from a JSON report, and supply the actual
applicable USD prices per million tokens:

```json
{
  "currency": "USD",
  "models": [
    {
      "provider": "openai",
      "endpoint": "https://api.example.com/v1",
      "model": "your-model",
      "source": "Your endpoint tariff and effective date",
      "input": null,
      "output": null,
      "cache_read": null,
      "cache_write": null
    }
  ]
}
```

`null` is unknown; explicit `0` means a known zero rate. Rates must be finite
and nonnegative. The exact provider/endpoint/model triple is the identity;
trailing endpoint slashes are ignored, credentials and query parameters are
not allowed. An absent file/entry leaves pricing unconfigured. Invalid pricing
is logged and marked unavailable without blocking the model request.

Every SDK attempt freezes its matching price before sending. Changing the file
affects later requests only; the report never loads today's file to reprice
older requests. This is an estimate using explicitly configured flat rates,
not currency conversion, tiered-price discovery or a provider invoice.

Uncached input, cached reads, cache writes and output are separate cost buckets.
Native Anthropic input excludes its separately reported cache buckets;
OpenAI-style totals include them. DeepSeek's reported cache-miss bucket is
uncached input, with no separately billed write bucket. Missing counters remain
unknown rather than zero. Embedding uses its input count alone. Reasoning is
already part of output and is not charged a second time. The report gives a
known subtotal and the number of incompletely priced calls; a complete amount
exists only when every applicable bucket is known.

Input source shares remain local reference-token counts, not dollar allocations.

## Cache comparisons

SDK attempts retain hashes of the ordered tool definitions, leading system
content, and cumulative input-item prefixes, together with request cache and
reasoning settings. They add no new text copies. Comparisons use completed,
non-overlapping requests from the same owner, provider endpoint, model, role,
activity, stage and evaluation class. Failed requests are not warm-cache
controls, and initial calls are not mixed with tool continuations.

The report records elapsed time, exact client-visible changes, shared input
items and actual reported cache reads. It does not infer provider cache expiry,
server warmup, internal serialization or the cause of a miss. A matching prefix
is an opportunity for reuse, not a promised hit. Cohort counts and weighted
cache-read fractions are observations, not randomized causal savings estimates.

## Operational boundary

Counts are computed before trace redaction, but no token IDs, new text copies,
or media bytes are persisted by this feature. Only the comparison
fingerprints above survive; source layouts live only for the current run.
Count failures are logged and marked unavailable;
they do not prevent the actual model call. `capture_ms` measures accounting
overhead.

`tiktoken` downloads its public vocabulary on first use. By default its cache
is `tokenizer-cache/` alongside the database, rather than an isolated or
restart-cleared system temporary directory. An existing `TIKTOKEN_CACHE_DIR`
is respected. Preload `mochi.token_distribution._encoding()` under the service
account during deployment so a chat need not wait for this download. Subsequent
counting is local; query-only access does not require loading the vocabulary.
