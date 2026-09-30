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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache, wraps
from pathlib import Path

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


_current: ContextVar[Run | None] = ContextVar("runtime_trace", default=None)


@dataclass
class Phase:
    name: str
    waiting_cancelled: bool = False


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
        if value.get("type") in {"base64", "input_audio"}:
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
            ["git", "status", "--porcelain", "--untracked-files=no"],
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


def _insert(run: Run, kind: str, name: str, request, *, span_id: str) -> bool:
    if _last_purge_day != _now()[:10]:
        purge()
    return _write(
        "INSERT INTO runtime_traces "
        "(trace_id,span_id,turn_id,user_id,run_kind,span_kind,name,process_id,"
        "status,request_json,started_at) VALUES (?,?,?,?,?,?,?,?,'running',?,?)",
        (
            run.trace_id, span_id, run.turn_id, run.user_id, run.kind,
            kind, name, PROCESS_ID, _payload(request), _now(),
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
def stage(name: str):
    phase = Phase(name)
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


@contextmanager
def span(kind: str, name: str, request=None, *, run: Run | None = None):
    active = run or _current.get()
    if active is None:
        yield None
        return
    item = Span(active, uuid.uuid4().hex, time.monotonic())
    phase = _stage.get()
    _insert(
        active, kind, f"{phase.name}:{name}" if phase else name,
        request, span_id=item.span_id,
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
            "THEN 'completed_late' ELSE ? END, response_json=?, error=?, "
            "finished_at=?, duration_ms=? WHERE span_id=?",
            (
                status, active.trace_id, status, _payload(item.response), error,
                _now(), (time.monotonic() - item.started) * 1000, item.span_id,
            ),
        )


def event(name: str, request=None, response=None) -> None:
    with span("event", name, request) as item:
        if item:
            item.response = response


def sdk_call(
    fn, *, protocol: str, provider: str, endpoint: str = "",
    client_timeout=None, **kwargs,
):
    """Record the exact SDK boundary, including negotiation attempts."""
    if _current.get() is None:
        from mochi.config import OWNER_USER_ID
        with run_scope(uuid.uuid4().hex, "provider", OWNER_USER_ID or 0, code_version()) as run:
            result = sdk_call(
                fn, protocol=protocol, provider=provider, endpoint=endpoint,
                client_timeout=client_timeout, **kwargs,
            )
            finish_run(run.trace_id, "completed")
            return result
    timeout = (
        client_timeout.as_dict() if hasattr(client_timeout, "as_dict") else client_timeout
    )
    with span("model", protocol, {
        "provider": provider, "endpoint": endpoint, "client_timeout": timeout, **kwargs,
    }) as item:
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
            request_id = getattr(response, "_request_id", None)
            item.response = (
                {"body": response, "request_id": request_id} if request_id else response
            )
        return response


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
        with span("delivery", self.name, {
            "text_chars": len(result.text), "stickers": len(result.stickers),
        }, run=run) as item:
            try:
                delivered = await fn(self, user_id, result, *args, **kwargs)
            except BaseException as exc:
                status = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError)
                    else getattr(exc, "outcome", "delivery_unknown")
                )
                record_delivery(result, status, detail=type(exc).__name__)
                raise
            if item:
                item.response = {"confirmed": bool(delivered)}
            record_delivery(result, "delivered" if delivered else "delivery_unknown")
            return delivered
    return wrapped


def list_runs(user_id: int, *, limit: int = 30, before: int | None = None) -> dict:
    from mochi.db import _connect

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id,trace_id,turn_id,run_kind,status,started_at,finished_at,duration_ms "
            "FROM runtime_traces WHERE span_kind='run' AND user_id=? "
            "AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
            (user_id, before, before, limit + 1),
        ).fetchall()
    finally:
        conn.close()
    page = [dict(row) for row in rows[:limit]]
    return {"runs": page, "next_before": page[-1]["id"] if len(rows) > limit else None}


def get_run(user_id: int, trace_id: str) -> dict | None:
    from mochi.db import _connect

    conn = _connect()
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
        tools = conn.execute(
            "SELECT id,tool_call_id,source,tool_name,action,arguments_json,status,"
            "result_summary,state_changed,started_at,finished_at "
            "FROM tool_executions WHERE turn_id=? AND user_id=? ORDER BY id",
            (root["turn_id"], user_id),
        ).fetchall()
    finally:
        conn.close()
    spans = []
    for row in rows:
        item = dict(row)
        for key in ("request_json", "response_json"):
            encoded = item.pop(key)
            item[key.removesuffix("_json")] = json.loads(encoded) if encoded else None
        spans.append(item)
    return {
        "trace_id": trace_id, "turn_id": root["turn_id"],
        "spans": spans, "tools": _sanitize([dict(row) for row in tools]),
        "retention_days": RETENTION_DAYS,
    }
