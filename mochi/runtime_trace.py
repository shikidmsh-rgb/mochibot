"""Human-only runtime evidence; never a source for Main context or memory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache, wraps
from pathlib import Path

from mochi.token_distribution import TextSource

log = logging.getLogger(__name__)
RETENTION_DAYS = 7
MAX_PAYLOAD_CHARS = 2_000_000
PROCESS_ID = uuid.uuid4().hex
_secrets: set[str] = set()
_last_purge_day = ""
_secret_key = re.compile(
    r"^(?:api[_-]?key|x-api-key|authorization|password|secret|token|access[_-]?token|"
    r"refresh[_-]?token|admin[_-]?token|bot[_-]?token|context[_-]?token|"
    r"cookie|set-cookie|credentials?)$", re.I,
)
_data_url = re.compile(r"data:[^;\s]+;base64,[A-Za-z0-9+/=_-]+")
_bearer = re.compile(r"\bBearer\s+\S+", re.I)
_api_key_text = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")
_credential_assignment = re.compile(
    r"""(\b(?:api[_-]?key|admin[_-]?token|access[_-]?token|password)\s*[:=]\s*)"""
    r"""(?:"[^"]*"|'[^']*'|[^\s,;]+)""", re.I,
)
_credential_query = re.compile(
    r"([?&](?:api[_-]?key|token|access_token|signature|x-amz-signature|sig)=)[^&#\s]+",
    re.I,
)


@dataclass(frozen=True)
class Run:
    trace_id: str
    turn_id: str
    user_id: int | None
    kind: str
    token_sources: list[TextSource] = field(default_factory=list)


_current: ContextVar[Run | None] = ContextVar("runtime_trace", default=None)


@dataclass
class Phase:
    name: str
    operation_id: str
    waiting_cancelled: bool = False
    model_role: str = ""
    purpose: str = ""
    usage_stage: str = ""


_stage: ContextVar[Phase | None] = ContextVar("runtime_trace_stage", default=None)


def register_secret(value: str) -> None:
    if value:
        _secrets.add(value)


def _clean_text(value: str) -> str:
    from mochi import config

    secrets = set(_secrets)
    for key, item in vars(config).items():
        if (
            key.isupper() and isinstance(item, str) and item
            and (key.endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET")))
        ):
            secrets.add(item)
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = _data_url.sub("[MEDIA OMITTED]", value)
    value = _bearer.sub("Bearer [REDACTED]", value)
    value = _api_key_text.sub("[REDACTED]", value)
    value = _credential_assignment.sub(r"\1[REDACTED]", value)
    return _credential_query.sub(r"\1[REDACTED]", value)


def _sanitize(value, *, key: str = ""):
    if _secret_key.fullmatch(key):
        return "[REDACTED]"
    if isinstance(value, bytes):
        return {"omitted": "binary", "bytes": len(value)}
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        if isinstance(value.get("type"), str) and value["type"] in {"base64", "input_audio"}:
            return {
                "omitted": "media",
                "type": value.get("type"),
                "media_type": value.get("media_type") or value.get("format"),
            }
        result = {}
        for name, item in value.items():
            if name in {"arguments", "args"} and value.get("name") == "manage_settings":
                if isinstance(item, str):
                    try:
                        parsed = json.loads(item)
                    except json.JSONDecodeError:
                        result[name] = "[REDACTED settings arguments]"
                        continue
                else:
                    parsed = item
                if isinstance(parsed, dict):
                    parsed = {**parsed}
                    if "value" in parsed:
                        parsed["value"] = "[REDACTED]"
                else:
                    parsed = "[REDACTED settings arguments]"
                result[name] = (
                    json.dumps(_sanitize(parsed), ensure_ascii=False)
                    if isinstance(item, str) else _sanitize(parsed)
                )
            else:
                result[str(name)] = _sanitize(item, key=str(name))
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                pass
            else:
                cleaned = _sanitize(parsed)
                return (
                    _clean_text(value) if cleaned == parsed
                    else json.dumps(cleaned, ensure_ascii=False)
                )
        return _clean_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _clean_text(str(value))


def serialize(value) -> str:
    encoded = json.dumps(_sanitize(value), ensure_ascii=False, allow_nan=False)
    if len(encoded) > MAX_PAYLOAD_CHARS:
        return json.dumps({
            "omitted": "payload exceeds trace storage limit",
            "chars": len(encoded),
            "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        })
    return encoded


def sanitize_evidence(value):
    """Apply trace redaction to an export without truncating the whole report."""
    return _sanitize(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(sql: str, args: tuple = ()) -> bool:
    from mochi.db import _connect

    try:
        conn = _connect()
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        log.exception("Runtime trace persistence failed")
        return False


def _payload(value) -> str | None:
    if value is None:
        return None
    try:
        return serialize(value)
    except Exception:
        log.exception("Runtime trace serialization failed")
        return '{"omitted":"trace serialization failed"}'


@lru_cache(maxsize=1)
def code_version() -> dict:
    from mochi._version import read_version

    root = Path(__file__).resolve().parent.parent
    result = {"version": read_version(), "commit": None, "tracked_changes": None}
    try:
        result["commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            stderr=subprocess.DEVNULL, timeout=3,
        ).strip()
        result["tracked_changes"] = bool(subprocess.check_output(
            ["git", "--no-optional-locks", "status", "--porcelain", "--untracked-files=no"],
            cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=3,
        ).strip())
    except (OSError, subprocess.SubprocessError):
        log.warning("Runtime trace could not identify the Git checkout")
    return result


def initialize_process() -> None:
    _write(
        "UPDATE runtime_traces SET status='interrupted', finished_at=?, "
        "error='Process ended before this work reached a recorded outcome' "
        "WHERE process_id != ? AND status IN ('running','prepared')",
        (_now(), PROCESS_ID),
    )
    purge()


def purge() -> None:
    global _last_purge_day
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    if _write("DELETE FROM runtime_traces WHERE started_at < ?", (cutoff,)):
        _last_purge_day = _now()[:10]


def _insert(run: Run, kind: str, name: str, request, *, span_id: str, facts=None) -> bool:
    if _last_purge_day != _now()[:10]:
        purge()
    return _write(
        "INSERT INTO runtime_traces "
        "(trace_id,span_id,turn_id,user_id,run_kind,span_kind,name,process_id,"
        "status,request_json,started_at,facts_json) VALUES (?,?,?,?,?,?,?,?,'running',?,?,?)",
        (
            run.trace_id, span_id, run.turn_id, run.user_id, run.kind,
            kind, name, PROCESS_ID, _payload(request), _now(), _payload(facts),
        ),
    )


def finish_run(trace_id: str, status: str, *, result=None, error=None) -> None:
    if not trace_id:
        return
    stamp = _now()
    _write(
        "UPDATE runtime_traces SET status=?, response_json=COALESCE(?,response_json), "
        "error=COALESCE(?,error), finished_at=?, "
        "duration_ms=(julianday(?) - julianday(started_at))*86400000 "
        "WHERE span_id=? AND span_kind='run'",
        (status, _payload(result), _clean_text(str(error)) if error else None,
         None if status == "prepared" else stamp, stamp, trace_id),
    )


def finish_turn(turn_id: str, status: str) -> None:
    from mochi.db import _connect

    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT trace_id FROM runtime_traces WHERE turn_id=? "
                "AND span_kind='run' ORDER BY id DESC LIMIT 1", (turn_id,),
            ).fetchone()
        finally:
            conn.close()
        if row:
            finish_run(row["trace_id"], status)
    except sqlite3.Error:
        log.exception("Runtime trace could not record turn outcome")


@contextmanager
def run_scope(turn_id: str, kind: str, user_id: int | None, request):
    run = Run(uuid.uuid4().hex, turn_id, user_id, kind)
    token = _current.set(run)
    _insert(run, "run", kind, request, span_id=run.trace_id)
    try:
        yield run
    except BaseException as exc:
        finish_run(
            run.trace_id, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        _current.reset(token)


@contextmanager
def stage(
    name: str, *, operation_id: str | None = None,
    model_role: str = "", purpose: str = "", usage_stage: str = "",
):
    phase = Phase(
        name, operation_id or uuid.uuid4().hex,
        model_role=model_role, purpose=purpose, usage_stage=usage_stage,
    )
    token = _stage.set(phase)
    try:
        yield
    except asyncio.CancelledError:
        phase.waiting_cancelled = True
        raise
    finally:
        _stage.reset(token)


@dataclass
class Span:
    run: Run
    span_id: str
    started: float
    response: object = None
    facts: dict | None = None


@contextmanager
def span(kind: str, name: str, request=None, *, run: Run | None = None, facts=None):
    active = run or _current.get()
    if active is None:
        yield None
        return
    item = Span(active, uuid.uuid4().hex, time.monotonic(), facts=facts)
    phase = _stage.get()
    _insert(
        active, kind, f"{phase.name}:{name}" if phase else name,
        request, span_id=item.span_id, facts=facts,
    )
    status, error = "completed", None
    try:
        yield item
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        error = _clean_text(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if kind == "model" and status == "completed" and phase and phase.waiting_cancelled:
            status = "completed_late"
        _write(
            "UPDATE runtime_traces SET status=CASE "
            "WHEN ?='completed' AND span_kind='model' "
            "AND EXISTS (SELECT 1 FROM runtime_traces r "
            "WHERE r.span_id=? AND r.status NOT IN ('running','prepared')) "
            "THEN 'completed_late' ELSE ? END, response_json=?, facts_json=?, error=?, "
            "finished_at=?, duration_ms=? WHERE span_id=?",
            (
                status, active.trace_id, status, _payload(item.response), _payload(item.facts), error,
                _now(), (time.monotonic() - item.started) * 1000, item.span_id,
            ),
        )


def event(name: str, request=None, response=None, *, facts=None) -> None:
    with span("event", name, request, facts=facts) as item:
        if item:
            item.response = response


async def prepare(name: str, function, *args, **kwargs):
    """Observe existing threaded preparation without changing its execution."""
    with span("preparation", name):
        return await asyncio.to_thread(function, *args, **kwargs)


async def prepare_async(name: str, awaitable):
    with span("preparation", name):
        return await awaitable


def prepare_sync(name: str, function, *args, **kwargs):
    with span("preparation", name):
        return function(*args, **kwargs)


def register_token_source(role: str, text: str, parts: list[tuple[str, str]]) -> None:
    active = _current.get()
    if active is None:
        return
    if "".join(body for _, body in parts) != text:
        log.error("Token source layout does not match the request text")
        return
    active.token_sources.append(TextSource(role, text, tuple(parts)))


def joined_system_token_source(messages: list[dict], text: str) -> None:
    """Preserve source ranges when the Anthropic adapter joins system messages."""
    active = _current.get()
    if active is None:
        return
    parts = []
    for message in messages:
        if message["role"] != "system":
            continue
        body = message["content"]
        original = next(
            (item.parts for item in active.token_sources if item.role == "system" and item.text == body),
            (("system", body),),
        )
        parts.extend(original)
        parts.append(("separator", "\n"))
    while parts and not parts[0][1].strip():
        parts.pop(0)
    while parts and not parts[-1][1].strip():
        parts.pop()
    if parts:
        parts[0] = (parts[0][0], parts[0][1].lstrip())
        parts[-1] = (parts[-1][0], parts[-1][1].rstrip())
    register_token_source("system", text, parts)


def sdk_call(
    fn, *, protocol: str, provider: str, endpoint: str = "",
    client_timeout=None, receipt: dict | None = None, **kwargs,
):
    """Record the exact SDK boundary, including negotiation attempts."""
    if _current.get() is None:
        from mochi.config import OWNER_USER_ID
        with run_scope(uuid.uuid4().hex, "provider", OWNER_USER_ID or 0, code_version()) as run:
            result = sdk_call(
                fn, protocol=protocol, provider=provider, endpoint=endpoint,
                client_timeout=client_timeout, receipt=receipt, **kwargs,
            )
            finish_run(run.trace_id, "completed")
            return result
    timeout = (
        client_timeout.as_dict() if hasattr(client_timeout, "as_dict") else client_timeout
    )
    phase = _stage.get()
    operation_id = phase.operation_id if phase else uuid.uuid4().hex
    from mochi.token_distribution import cache_fingerprint, record_request, record_usage
    from mochi.token_accounting import record_usage_units
    distribution = record_request(kwargs, _current.get().token_sources)
    billing = {
        "provider": provider, "endpoint": endpoint, "model": kwargs.get("model"),
        "protocol": protocol,
    }
    facts = {
        "kind": "model", "operation_id": operation_id, "token_distribution": distribution,
        "billing": billing,
        "model_role": "EMBEDDING" if protocol == "embeddings.create" else phase.model_role if phase else "",
        "purpose": "embedding" if protocol == "embeddings.create" else phase.purpose if phase else "",
        "usage_stage": phase.usage_stage if phase else "",
    }
    try:
        facts["cache_fingerprint"] = cache_fingerprint(kwargs)
    except (TypeError, ValueError):
        log.exception("Could not fingerprint the client-visible cache prefix")
        facts["cache_fingerprint"] = {"status": "unavailable"}
    with span("model", protocol, {
        "provider": provider, "endpoint": endpoint, "client_timeout": timeout, **kwargs,
    }, facts=facts) as item:
        if receipt is not None and item is not None:
            receipt["span_id"] = item.span_id
        try:
            response = fn(**kwargs)
        except Exception as exc:
            if item:
                item.response = {
                    "status_code": getattr(exc, "status_code", None),
                    "body": getattr(exc, "body", None),
                    "request_id": getattr(exc, "request_id", None),
                }
            raise
        if item:
            record_usage(distribution, response)
            record_usage_units(billing, distribution.get("provider_usage"), protocol)
            request_id = getattr(response, "_request_id", None)
            item.response = (
                {"body": response, "request_id": request_id} if request_id else response
            )
            try:
                from mochi.execution_diagnostics import model_facts
                item.facts.update(model_facts(
                    protocol=protocol, endpoint=endpoint, request=kwargs,
                    response=response, operation_id=operation_id,
                ))
            except Exception:
                log.exception("Could not summarize model terminal evidence")
        return response


def start_tool(execution_id: int, tool: str, arguments: dict, *, document_target=None) -> Span | None:
    run = _current.get()
    if run is None:
        return None
    from mochi.tool_execution import sanitize_arguments

    operation_id = None
    try:
        if document_target is not None:
            identity = {"tool": tool, "document": document_target}
        else:
            safe = _sanitize(sanitize_arguments(tool, arguments))
            identity = {"tool": tool, "arguments": arguments} if safe == arguments else None
        if identity is not None:
            operation_id = hashlib.sha256(
                json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
    except Exception:
        log.exception("Could not identify a diagnostic tool retry")
    facts = {
        "kind": "tool", "execution_id": execution_id, "operation_id": operation_id,
        "state_change_unknown": True,
    }
    item = Span(run, uuid.uuid4().hex, time.monotonic(), facts=facts)
    _insert(run, "tool", tool, {"execution_id": execution_id}, span_id=item.span_id, facts=facts)
    return item


def finish_tool(item: Span | None, result) -> None:
    if item is None:
        return
    facts = {
        **item.facts, "success": result.success, "error_code": result.error_code,
        "state_changed": result.state_changed,
        "state_change_unknown": result.state_change_unknown,
        "execution_started": result.execution_started,
    }
    _write(
        "UPDATE runtime_traces SET status=?,facts_json=?,finished_at=?,duration_ms=? "
        "WHERE span_id=?",
        (
            "completed" if result.success else "failed", _payload(facts),
            _now(), (time.monotonic() - item.started) * 1000, item.span_id,
        ),
    )


def tool_rejection_facts(calls: list[dict], results: list[dict], executed: set[str]) -> dict | None:
    try:
        names = {call["id"]: call["name"] for call in calls}
        rejected = []
        for result in results:
            call_id = result["tool_call_id"]
            if call_id in executed:
                continue
            payload = json.loads(result["content"])
            if payload.get("ok") is False:
                rejected.append({
                    "call_id": call_id, "tool": names[call_id], "code": payload.get("code"),
                })
        return {"kind": "rejections", "items": rejected} if rejected else None
    except (KeyError, ValueError, AttributeError):
        log.exception("Could not summarize rejected tool calls")
        return None


def _read_diagnostics(conn, roots: list[dict]) -> dict[str, dict]:
    from mochi.execution_diagnostics import summarize

    if not roots:
        return {}
    trace_ids = [row["trace_id"] for row in roots]
    placeholders = ",".join("?" for _ in trace_ids)
    rows = conn.execute(
        "SELECT id,trace_id,span_id,span_kind,name,status,started_at,finished_at,facts_json "
        f"FROM runtime_traces WHERE trace_id IN ({placeholders}) ORDER BY id",
        trace_ids,
    ).fetchall()
    grouped = {trace_id: [] for trace_id in trace_ids}
    for row in rows:
        item = dict(row)
        raw = item.pop("facts_json")
        item["facts"] = json.loads(raw) if raw else None
        grouped[item["trace_id"]].append(item)
    # The ledger remains authoritative; explicit execution IDs prevent repeated
    # invocations sharing a turn_id from inheriting one another's successes.
    tool_rows = conn.execute(
        "SELECT t.trace_id,e.id,e.tool_name,e.status,e.state_changed,e.started_at,e.finished_at "
        "FROM runtime_traces t JOIN tool_executions e "
        "ON e.id=json_extract(t.facts_json,'$.execution_id') AND e.user_id=t.user_id "
        f"WHERE t.span_kind='tool' AND t.trace_id IN ({placeholders}) ORDER BY e.id",
        trace_ids,
    ).fetchall()
    tools = {trace_id: [] for trace_id in trace_ids}
    for row in tool_rows:
        tools[row["trace_id"]].append(dict(row))
    summaries = {}
    for root in roots:
        trace_id = root["trace_id"]
        # Old traces lack execution IDs. Preserve their failures as evidence,
        # but do not infer recovery relationships from redacted argument text.
        if not any(row["span_kind"] == "tool" for row in grouped[trace_id]):
            tools[trace_id] = [dict(row) for row in conn.execute(
                "SELECT id,tool_name,status,state_changed,started_at,finished_at "
                "FROM tool_executions WHERE turn_id=? AND user_id=? "
                "AND julianday(started_at)>=julianday(?) "
                "AND julianday(started_at)<=julianday(COALESCE(?,?)) ORDER BY id",
                (root["turn_id"], root["user_id"], root["started_at"],
                 root["finished_at"], _now()),
            )]
        summaries[trace_id] = summarize(root, grouped[trace_id], tools[trace_id])
    return summaries


def record_delivery(result, status: str, *, detail=None) -> None:
    trace_id = getattr(result, "_trace_id", "")
    if not trace_id:
        return
    if not getattr(result, "_trace_component", False):
        with span(
            "delivery", "outcome", {"status": status, "detail": detail},
            run=Run(trace_id, "", None, "delivery"),
        ):
            pass
        finish_run(trace_id, status, error=detail)


def delivery_method(fn):
    @wraps(fn)
    async def wrapped(self, user_id, result, *args, **kwargs):
        trace_id = getattr(result, "_trace_id", "")
        if not trace_id:
            return await fn(self, user_id, result, *args, **kwargs)
        run = Run(trace_id, "", user_id, "delivery")
        component = getattr(result, "_trace_component", False)
        operation_id = (
            hashlib.sha256(json.dumps(
                {"text": result.text, "stickers": result.stickers},
                ensure_ascii=False, sort_keys=True,
            ).encode()).hexdigest() if component else "whole_reply"
        )
        with span("delivery", self.name, {
            "text_chars": len(result.text), "stickers": len(result.stickers),
        }, run=run, facts={
            "kind": "delivery_attempt", "operation_id": operation_id,
        }) as item:
            try:
                delivered = await fn(self, user_id, result, *args, **kwargs)
            except BaseException as exc:
                status = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError)
                    else getattr(exc, "outcome", "delivery_unknown")
                )
                if item:
                    item.facts.update(outcome=status, confirmed=False)
                record_delivery(result, status, detail=type(exc).__name__)
                raise
            if item:
                item.response = {"confirmed": bool(delivered)}
                item.facts.update(
                    confirmed=bool(delivered),
                    outcome="delivered" if delivered else "delivery_unknown",
                )
            record_delivery(result, "delivered" if delivered else "delivery_unknown")
            return delivered
    return wrapped


def read_connection() -> sqlite3.Connection:
    """Open existing evidence without creating or configuring a database."""
    from mochi.db import DB_PATH

    conn = sqlite3.connect(Path(DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def list_runs(
    user_id: int, *, limit: int = 30, before: int | None = None,
    since: str | None = None, until: str | None = None, kind: str | None = None,
) -> dict:
    conn = read_connection()
    try:
        rows = conn.execute(
            "SELECT id,trace_id,span_id,user_id,turn_id,run_kind,status,started_at,finished_at,duration_ms "
            "FROM runtime_traces WHERE span_kind='run' AND user_id=? "
            "AND (? IS NULL OR id < ?) "
            "AND (? IS NULL OR julianday(started_at)>=julianday(?)) "
            "AND (? IS NULL OR julianday(started_at)<=julianday(?)) "
            "AND (? IS NULL OR run_kind=?) ORDER BY id DESC LIMIT ?",
            (user_id, before, before, since, since, until, until, kind, kind, limit + 1),
        ).fetchall()
        page = [dict(row) for row in rows[:limit]]
        diagnostics = _read_diagnostics(conn, page)
        for row in page:
            summary = diagnostics[row["trace_id"]]
            row["diagnostics"] = {key: value for key, value in summary.items() if key != "issues"}
    finally:
        conn.close()
    return {"runs": page, "next_before": page[-1]["id"] if len(rows) > limit else None}


def get_run(user_id: int, trace_id: str) -> dict | None:
    conn = read_connection()
    try:
        root = conn.execute(
            "SELECT * FROM runtime_traces WHERE span_id=? AND span_kind='run' AND user_id=?",
            (trace_id, user_id),
        ).fetchone()
        if root is None:
            return None
        rows = conn.execute(
            "SELECT * FROM runtime_traces WHERE trace_id=? ORDER BY id", (trace_id,),
        ).fetchall()
        tool_ids = [
            json.loads(row["facts_json"])["execution_id"]
            for row in rows if row["span_kind"] == "tool" and row["facts_json"]
        ]
        if tool_ids:
            tool_filter = f"AND id IN ({','.join('?' for _ in tool_ids)})"
            tool_params = tuple(tool_ids)
        else:
            tool_filter = (
                "AND julianday(started_at)>=julianday(?) "
                "AND julianday(started_at)<=julianday(COALESCE(?,?))"
            )
            tool_params = (root["started_at"], root["finished_at"], _now())
        tools = conn.execute(
            "SELECT id,tool_call_id,source,tool_name,action,arguments_json,status,"
            "result_summary,state_changed,started_at,finished_at "
            f"FROM tool_executions WHERE turn_id=? AND user_id=? {tool_filter} ORDER BY id",
            (root["turn_id"], user_id, *tool_params),
        ).fetchall()
        diagnostics = _read_diagnostics(conn, [dict(root)])[trace_id]
    finally:
        conn.close()
    spans = []
    for row in rows:
        item = dict(row)
        for key in ("request_json", "response_json"):
            encoded = item.pop(key)
            item[key.removesuffix("_json")] = json.loads(encoded) if encoded else None
        encoded = item.pop("facts_json")
        item["facts"] = json.loads(encoded) if encoded else None
        spans.append(item)
    return {
        "trace_id": trace_id, "turn_id": root["turn_id"],
        "spans": spans, "tools": _sanitize([dict(row) for row in tools]),
        "retention_days": RETENTION_DAYS,
        "diagnostics": diagnostics,
    }


def query_token_distribution(
    user_id: int, *, trace_id: str | None = None, since: str | None = None,
    until: str | None = None, kind: str | None = None,
    before: int | None = None, limit: int = 30,
) -> dict:
    """Read original per-attempt counts; never reconstruct old/redacted payloads."""
    from mochi.token_distribution import summarize

    if not 1 <= limit <= 100 or (before is not None and before <= 0):
        raise ValueError("Invalid token distribution page")
    bounds = []
    for value in (since, until):
        instant = datetime.fromisoformat(value) if value is not None else None
        if instant is not None and instant.utcoffset() is None:
            raise ValueError("Token distribution timestamps require a timezone")
        bounds.append(instant)
    if all(value is not None for value in bounds) and bounds[0] > bounds[1]:
        raise ValueError("Token distribution since must not be after until")
    conn = read_connection()
    try:
        rows = conn.execute(
            "SELECT id,trace_id,span_id,run_kind,name,status,started_at,facts_json "
            "FROM runtime_traces WHERE span_kind='model' AND user_id=? "
            "AND (? IS NULL OR trace_id=?) AND (? IS NULL OR id<?) "
            "AND (? IS NULL OR julianday(started_at)>=julianday(?)) "
            "AND (? IS NULL OR julianday(started_at)<=julianday(?)) "
            "AND (? IS NULL OR run_kind=?) ORDER BY id DESC LIMIT ?",
            (user_id, trace_id, trace_id, before, before, since, since, until, until, kind, kind, limit + 1),
        ).fetchall()
    finally:
        conn.close()
    calls = []
    for row in rows[:limit]:
        call = dict(row)
        facts = json.loads(call.pop("facts_json") or "{}")
        call["operation_id"] = facts.get("operation_id")
        call["distribution"] = facts.get("token_distribution") or {
            "status": "unavailable", "reason": "not_recorded",
        }
        calls.append(call)
    return {
        "calls": calls, "summary": summarize(calls),
        "next_before": calls[-1]["id"] if len(rows) > limit else None,
        "retention_days": RETENTION_DAYS,
    }
