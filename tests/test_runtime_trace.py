import asyncio
import json
import sqlite3
import threading

import httpx
import pytest

from mochi import runtime_trace as trace
from mochi.db import _connect


@pytest.fixture(autouse=True)
def trace_state(monkeypatch):
    monkeypatch.setattr(trace, "_secrets", set())
    monkeypatch.setattr(trace, "_last_purge_day", "")


def test_request_is_durable_before_work_and_media_secrets_are_not_stored():
    credential = "private-provider-credential"
    trace.register_secret(credential)
    arguments = {
        "max_tokens": 128,
        "messages": [{
            "role": "user",
            "content": f"Credential {credential}; data:image/png;base64,aGVsbG8=",
        }],
        "previous": {
            "name": "manage_settings",
            "arguments": '{"action":"set","value":"new-unregistered-secret"}',
        },
        "extra_headers": {"Authorization": "Bearer arbitrary-token"},
        "note": "api_key='unregistered-key-value'",
    }
    original = json.dumps(arguments)
    with trace.run_scope("turn-before-call", "free_time", 0, {"setting": 128}) as run:
        def perform(**kwargs):
            evidence = trace.get_run(0, run.trace_id)
            current = evidence["spans"][-1]
            assert current["status"] == "running"
            assert current["request"]["max_tokens"] == 128
            return {"measured_messages": len(kwargs["messages"])}

        trace.sdk_call(perform, protocol="test", provider="test", **arguments)
        trace.finish_run(run.trace_id, "skip")

    persisted = trace.get_run(0, run.trace_id)
    encoded = json.dumps(persisted)
    assert credential not in encoded
    assert "new-unregistered-secret" not in encoded
    assert "arbitrary-token" not in encoded
    assert "unregistered-key-value" not in encoded
    assert "aGVsbG8=" not in encoded
    assert "[REDACTED]" in encoded and "MEDIA OMITTED" in encoded
    assert persisted["spans"][1]["status"] == "completed"
    assert persisted["spans"][1]["duration_ms"] >= 0
    assert json.dumps(arguments) == original
    assert trace.get_run(1, run.trace_id) is None
    assert trace.list_runs(1)["runs"] == []


def test_failure_keeps_response_evidence_without_turning_it_into_success():
    class Rejected(RuntimeError):
        status_code = 400
        body = {"error": "unsupported option", "api_key": "server-echoed-secret"}
        request_id = "request-rejected"

    def reject(**kwargs):
        raise Rejected("server rejected this request")

    with pytest.raises(Rejected):
        with trace.run_scope("rejected-turn", "chat", 1, {}) as run:
            trace.sdk_call(reject, protocol="test", provider="test", budget=128)
    evidence = trace.get_run(1, run.trace_id)
    assert evidence["spans"][0]["status"] == "failed"
    call = evidence["spans"][1]
    assert call["status"] == "failed"
    assert call["request"]["budget"] == 128
    assert call["response"]["status_code"] == 400
    assert call["response"]["request_id"] == "request-rejected"
    assert "server-echoed-secret" not in json.dumps(evidence)


@pytest.mark.asyncio
async def test_cancelled_wait_keeps_late_provider_result_without_reopening_run():
    started = threading.Event()
    release = threading.Event()
    settled = threading.Event()
    run_ids = []

    def work():
        started.set()
        if not release.wait(3):
            raise TimeoutError("test did not release worker")
        return {"finished_after_wait": True}

    def call():
        try:
            return trace.sdk_call(work, protocol="test", provider="test")
        finally:
            settled.set()

    async def waiting():
        with trace.run_scope("cancelled-turn", "bedtime", 1, {}) as run:
            run_ids.append(run.trace_id)
            await asyncio.to_thread(call)

    task = asyncio.create_task(waiting())
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        evidence = trace.get_run(1, run_ids[0])
        assert [item["status"] for item in evidence["spans"]] == ["cancelled", "running"]
    finally:
        release.set()
        assert await asyncio.to_thread(settled.wait, 3)
    evidence = trace.get_run(1, run_ids[0])
    assert [item["status"] for item in evidence["spans"]] == [
        "cancelled", "completed_late",
    ]
    assert evidence["spans"][1]["response"] is not None


def test_restart_marks_unfinished_work_and_retention_preserves_recent_evidence(monkeypatch):
    with trace.run_scope("old-turn", "chat", 1, {"context": "kept"}) as old:
        trace.finish_run(old.trace_id, "prepared")
    with trace.run_scope("expired-turn", "chat", 1, {}) as expired:
        trace.finish_run(expired.trace_id, "delivered")
    conn = _connect()
    conn.execute(
        "UPDATE runtime_traces SET started_at='2000-01-01T00:00:00+00:00' "
        "WHERE trace_id=?", (expired.trace_id,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(trace, "PROCESS_ID", "new-process")
    with trace.run_scope("new-turn", "chat", 1, {}) as current:
        trace.initialize_process()
        assert trace.get_run(1, current.trace_id)["spans"][0]["status"] == "running"
    evidence = trace.get_run(1, old.trace_id)
    assert evidence["spans"][0]["status"] == "interrupted"
    assert evidence["spans"][0]["request"]["context"] == "kept"
    assert trace.get_run(1, expired.trace_id) is None


@pytest.mark.asyncio
async def test_cancelled_router_response_is_late_while_main_is_still_running():
    started, release = threading.Event(), threading.Event()

    def slow_call():
        started.set()
        if not release.wait(3):
            raise TimeoutError("test worker was not released")
        return {"finished": True}

    with trace.run_scope("router-cancel", "chat", 1, {}) as run:
        try:
            with pytest.raises(asyncio.CancelledError):
                with trace.stage("router"):
                    pending = asyncio.create_task(asyncio.to_thread(
                        trace.sdk_call, slow_call, protocol="test", provider="test",
                    ))
                    assert await asyncio.to_thread(started.wait, 3)
                    raise asyncio.CancelledError()
        finally:
            release.set()
        await pending
        evidence = trace.get_run(1, run.trace_id)
        assert evidence["spans"][0]["status"] == "running"
        assert evidence["spans"][1]["status"] == "completed_late"
        trace.finish_run(run.trace_id, "skip")


@pytest.mark.asyncio
async def test_trace_api_requires_auth_scopes_owner_and_paginates(monkeypatch):
    import mochi.config as config
    from mochi.admin.admin_server import app

    monkeypatch.setattr(config, "ADMIN_TOKEN", "trace-admin-secret")
    monkeypatch.setattr(config, "OWNER_USER_ID", 0)
    ids = []
    for index in range(3):
        with trace.run_scope(f"page-{index}", "free_time", 0, {}) as run:
            ids.append(run.trace_id)
            trace.finish_run(run.trace_id, "skip")
    with trace.run_scope("different-owner", "chat", 2, {}) as foreign:
        trace.finish_run(foreign.trace_id, "skip")
    headers = {"Authorization": "Bearer trace-admin-secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("198.51.100.10", 1234)),
        base_url="http://test",
    ) as client:
        denied = await client.get("/api/diagnostics/traces")
        assert denied.status_code == 401
        first = (await client.get("/api/diagnostics/traces?limit=2", headers=headers)).json()
        second = (await client.get(
            f"/api/diagnostics/traces?limit=2&before={first['next_before']}",
            headers=headers,
        )).json()
        assert [row["trace_id"] for row in first["runs"] + second["runs"]] == ids[::-1]
        assert second["next_before"] is None
        hidden = await client.get(f"/api/diagnostics/traces/{foreign.trace_id}", headers=headers)
        assert hidden.status_code == 404
        detail = await client.get(f"/api/diagnostics/traces/{ids[0]}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["spans"][0]["status"] == "skip"


def test_oversize_trace_is_explicit_and_does_not_silently_truncate(monkeypatch):
    monkeypatch.setattr(trace, "MAX_PAYLOAD_CHARS", 50)
    record = json.loads(trace.serialize({"body": "x" * 60}))
    assert record["omitted"] == "payload exceeds trace storage limit"
    assert record["chars"] > 50
    assert len(record["sha256"]) == 64


@pytest.mark.asyncio
async def test_preparation_failure_and_cancelled_wait_remain_visible():
    def broken_read():
        raise sqlite3.OperationalError("read failed")

    entered = asyncio.Event()

    async def waiting():
        entered.set()
        await asyncio.Event().wait()

    with trace.run_scope("preparation-boundaries", "chat", 1, {}) as run:
        with pytest.raises(sqlite3.OperationalError):
            await trace.prepare("conversation", broken_read)
        pending = asyncio.create_task(trace.prepare_async("router", waiting()))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        trace.finish_run(run.trace_id, "skip")
    evidence = trace.get_run(1, run.trace_id)
    stages = [row for row in evidence["spans"] if row["span_kind"] == "preparation"]
    assert [(row["name"], row["status"]) for row in stages] == [
        ("conversation", "failed"), ("router", "cancelled"),
    ]
    assert all(row["duration_ms"] >= 0 for row in stages)
    assert "OperationalError" in stages[0]["error"]
    assert {issue["code"] for issue in evidence["diagnostics"]["issues"]} == {
        "preparation_failed", "preparation_unfinished",
    }
