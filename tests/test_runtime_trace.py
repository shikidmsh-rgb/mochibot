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
        "catalog": {"type": {"value": "plain", "api_key": "nested-secret"}},
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
    assert persisted["spans"][1]["request"]["catalog"] == {
        "type": {"value": "plain", "api_key": "[REDACTED]"},
    }
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


def test_token_distribution_counts_original_text_before_redaction_and_before_call():
    from mochi.token_distribution import _encoding

    secret = "private-long-credential-with-several-pieces"
    body = f"hello {secret} <|endoftext|>"
    trace.register_secret(secret)
    arguments = {"model": "reference-model", "messages": [{"role": "user", "content": body}]}
    original = json.dumps(arguments)
    with trace.run_scope("token-original", "chat", 1, {}) as run:
        def work(**kwargs):
            page = trace.query_token_distribution(1, trace_id=run.trace_id)
            call = page["calls"][0]
            assert call["status"] == "running"
            counts = call["distribution"]["totals"]
            assert counts["chars"] == len(body)
            assert counts["utf8_bytes"] == len(body.encode("utf-8"))
            assert counts["reference_tokens"] == len(_encoding().encode_ordinary(body))
            assert counts["reference_tokens"] != len(_encoding().encode_ordinary(
                body.replace(secret, "[REDACTED]"),
            ))
            return {}

        trace.sdk_call(work, protocol="test", provider="test", **arguments)
        trace.finish_run(run.trace_id, "skip")
    assert json.dumps(arguments) == original
    page = trace.query_token_distribution(1, trace_id=run.trace_id)
    assert secret not in json.dumps(page)
    assert page["summary"]["calls_with_provider_usage"] == 0
    assert page["calls"][0]["distribution"]["provider_usage"] is None


def test_prompt_source_ranges_preserve_bytes_and_distinguish_repeated_values(monkeypatch):
    from datetime import datetime, timezone
    from mochi import ai_client
    from mochi.token_distribution import capture_request

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 3, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(ai_client, "datetime", Clock)
    monkeypatch.setattr(ai_client, "get_system_chat_modules", lambda: {
        "agent": "prefix\n## capabilities\n{{capability_context}}\n## requestable\n{{requestable_tools}}",
    })
    shared = "Repeated context \u4eca\u5929 \U0001f431"
    options = {
        "core_memory": shared, "capability_context": shared,
        "conv_summary": shared, "requestable_tools": "",
    }
    plain = ai_client._build_prompt_zones(1, **options)
    layouts = {}
    observed = ai_client._build_prompt_zones(1, token_parts=layouts, **options)
    assert observed == plain
    for index, zone in enumerate(("system", "turn_context")):
        assert "".join(body for _, body in layouts[zone]) == plain[index]
    with trace.run_scope("source-layout", "chat", 1, {}) as run:
        trace.register_token_source("system", observed[0], layouts["system"])
        trace.register_token_source("user", observed[1], layouts["turn_context"])
        captured = capture_request({
            "messages": [
                {"role": "system", "content": observed[0]},
                {"role": "user", "content": observed[1]},
                {"role": "user", "content": shared},
            ],
        }, run.token_sources)
        sources = captured["parts"]
        assert sum(p["chars"] for p in sources if p["source"] == "core") == len(shared)
        assert sum(p["chars"] for p in sources if p["source"] == "agent.capability_context") == len(shared)
        assert sum(p["chars"] for p in sources if p["source"] == "message.user") == len(shared)
        assert captured["totals"]["chars"] == sum(map(len, observed)) + len(shared)
        assert captured["totals"]["utf8_bytes"] == sum(
            len(text.encode("utf-8")) for text in (*observed, shared)
        )
        trace.finish_run(run.trace_id, "skip")


def test_joined_system_attribution_preserves_stripping_and_separators():
    from mochi.token_distribution import capture_request

    messages = [
        {"role": "system", "content": "  core\n\nagent  "},
        {"role": "user", "content": "not system"},
        {"role": "system", "content": "later  "},
    ]
    body = "".join(m["content"] + "\n" for m in messages if m["role"] == "system").strip()
    with trace.run_scope("joined-system", "chat", 1, {}) as run:
        trace.register_token_source("system", messages[0]["content"], [
            ("core", "  core"), ("separator", "\n\n"), ("agent", "agent  "),
        ])
        trace.joined_system_token_source(messages, body)
        captured = capture_request({"system": body}, run.token_sources)
        assert captured["totals"]["chars"] == len(body)
        assert [p["source"] for p in captured["parts"]] == [
            "core", "separator", "agent", "separator", "system",
        ]
        assert captured["parts"][0]["chars"] == len("core")
        trace.finish_run(run.trace_id, "skip")


def test_reference_total_does_not_change_when_text_is_split_into_sources():
    from mochi.token_distribution import TextSource, capture_request

    text = "hello \u4eca\u5929 \U0001f431"
    plain = capture_request({"system": text}, [])
    source = TextSource("system", text, (
        ("left", "hel"), ("right", text[3:]),
    ))
    split = capture_request({"system": text}, [source])
    assert split["totals"]["reference_tokens"] == plain["totals"]["reference_tokens"]
    assert split["totals"]["chars"] == plain["totals"]["chars"]
    assert split["totals"]["utf8_bytes"] == plain["totals"]["utf8_bytes"]
    assert split["parts"][0]["reference_tokens"] == 1
    assert split["parts"][0]["cross_boundary_tokens"] == 1


def test_identical_history_and_current_input_keep_occurrence_ownership():
    from mochi.token_distribution import TextSource, capture_request

    result = capture_request({
        "messages": [{"role": "user", "content": "hello"} for _ in range(2)],
    }, [
        TextSource("user", "hello", (("history.user", "hello"),)),
        TextSource("user", "hello", (("input.current", "hello"),)),
    ])
    assert [part["source"] for part in result["parts"]] == ["history.user", "input.current"]
    assert result["totals"]["reference_tokens"] == 2


def test_opaque_inputs_are_not_tokens_and_tool_results_keep_separate_ownership():
    from mochi.token_distribution import capture_request

    result = capture_request({
        "input": [
            {"type": "function_call", "name": "read_note", "call_id": "a", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "a", "output": "note contents"},
            {"type": "reasoning", "encrypted_content": "opaque-ciphertext"},
            {"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8="}]},
        ],
        "tools": [{"name": "read_note", "parameters": {"type": "object"}}],
    }, [])
    parts = result["parts"]
    assert result["totals"]["uncounted_parts"] == 2
    assert all(p["reference_tokens"] is None for p in parts if p["representation"] == "opaque")
    assert result["totals"]["reference_tokens"] == sum(
        p["reference_tokens"] for p in parts if p["representation"] != "opaque"
    )
    by_source = {p["source"]: p for p in parts}
    assert by_source["tool_result.read_note"]["chars"] == len("note contents")
    assert by_source["tool_schema.read_note"]["representation"] == "canonical_json"
    assert "opaque-ciphertext" not in json.dumps(result)
    assert "aGVsbG8=" not in json.dumps(result)


def test_token_query_keeps_retries_missing_evidence_and_owner_boundaries(monkeypatch):
    with trace.run_scope("attempt-counts", "free_time", 1, {}) as run:
        with trace.stage("round_1", operation_id="round_1"):
            with pytest.raises(RuntimeError):
                trace.sdk_call(
                    lambda **kw: (_ for _ in ()).throw(RuntimeError("failed")),
                    protocol="test", provider="test", input="first",
                )
            trace.sdk_call(lambda **kw: {}, protocol="test", provider="test", input="first")
        with trace.stage("round_2", operation_id="round_2"):
            trace.sdk_call(lambda **kw: {}, protocol="test", provider="test", input="first + tool output")
        trace.finish_run(run.trace_id, "skip")
    with trace.run_scope("unrelated-owner", "free_time", 2, {}) as foreign:
        trace.sdk_call(lambda **kw: {}, protocol="test", provider="test", input="private")
        trace.finish_run(foreign.trace_id, "skip")
    conn = _connect()
    conn.execute(
        "UPDATE runtime_traces SET facts_json=NULL WHERE trace_id=? AND name='round_1:test' AND status='failed'",
        (run.trace_id,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(trace, "_write", lambda *a, **k: pytest.fail("query wrote evidence"))
    first = trace.query_token_distribution(1, trace_id=run.trace_id, kind="free_time", limit=2)
    second = trace.query_token_distribution(1, trace_id=run.trace_id, before=first["next_before"])
    calls = first["calls"] + second["calls"]
    assert [call["status"] for call in calls] == ["completed", "completed", "failed"]
    assert [call["operation_id"] for call in calls[:2]] == ["round_2", "round_1"]
    assert first["summary"]["calls"] == 2
    assert second["summary"]["recorded_calls"] == 0
    assert second["calls"][0]["distribution"]["status"] == "unavailable"
    assert second["next_before"] is None
    assert trace.query_token_distribution(2, trace_id=run.trace_id)["calls"] == []
    assert trace.query_token_distribution(
        1, until="2000-01-01T00:00:00+00:00",
    )["calls"] == []
    with pytest.raises(ValueError):
        trace.query_token_distribution(1, since="2026-10-03")
    with pytest.raises(ValueError):
        trace.query_token_distribution(
            1, since="2026-10-04T00:00:00Z", until="2026-10-03T00:00:00Z",
        )


def test_counter_failure_is_explicit_and_does_not_prevent_execution(monkeypatch, caplog):
    from mochi import token_distribution

    def unavailable():
        raise OSError("tokenizer is unavailable")

    monkeypatch.setattr(token_distribution, "_encoding", unavailable)
    executed = []
    with trace.run_scope("counter-failed", "chat", 1, {}) as run:
        trace.sdk_call(lambda **kw: executed.append(True), protocol="test", provider="test", input="hello")
        trace.finish_run(run.trace_id, "skip")
    assert executed == [True]
    recorded = trace.query_token_distribution(1, trace_id=run.trace_id)["calls"][0]
    assert recorded["distribution"]["status"] == "unavailable"
    assert "Could not record token distribution" in caplog.text


def test_tokenizer_cache_survives_isolated_service_tmp(fresh_db, monkeypatch):
    import os
    from mochi import token_distribution

    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    token_distribution._encoding.cache_clear()
    try:
        token_distribution._encoding()
        assert os.environ["TIKTOKEN_CACHE_DIR"] == str(fresh_db.parent / "tokenizer-cache")
    finally:
        token_distribution._encoding.cache_clear()


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
