import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mochi import runtime_trace as trace
from mochi.db import _connect
from scripts import diagnose


def record(turn, kind, user, stamp, status):
    with trace.run_scope(turn, kind, user, {"historical_input": turn}) as run:
        trace.finish_run(run.trace_id, status)
    conn = _connect()
    conn.execute(
        "UPDATE runtime_traces SET started_at=?,finished_at=? WHERE trace_id=?",
        (stamp, stamp, run.trace_id),
    )
    conn.commit()
    conn.close()
    return run.trace_id


def test_command_filters_before_limit_and_never_writes_to_database(monkeypatch, capsys):
    stamps = ["2026-10-01T04:00:00+00:00", "2026-10-01T12:01:00+08:00", "2026-10-01T04:02:00+00:00"]
    failed = record("failed-target", "free_time", 1, stamps[0], "failed")
    record("other-kind", "chat", 1, stamps[1], "failed")
    record("other-user", "free_time", 2, stamps[1], "failed")
    record("latest-ok", "free_time", 1, stamps[2], "skip")
    monkeypatch.setattr(trace, "_write", lambda *a, **k: pytest.fail("read-only command wrote evidence"))
    result = diagnose.main([
        "--since", "2026-10-01T11:59:00+08:00", "--until", "2026-10-01T12:03:00+08:00",
        "--kind", "free_time", "--state", "unresolved", "--limit", "1",
    ])
    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert [run["trace_id"] for run in report["runs"]] == [failed]
    assert report["scanned_runs"] == 2
    assert report["journal"]["status"] == "not_requested"
    assert "records" not in report
    with trace.read_connection() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM runtime_traces")


def test_export_uses_recorded_input_redacts_logs_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    stamp = datetime.now(timezone.utc).isoformat()
    identifier = record("original-input", "chat", 1, stamp, "delivered")
    conn = _connect()
    conn.execute(
        "INSERT INTO model_registry (name,provider,model,api_key,base_url,created_at,updated_at) "
        "VALUES ('redaction-model','openai','model','raw-export-secret','',?,?)",
        (stamp, stamp),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(diagnose, "read_journal", lambda *a: {
        "status": "available", "events": [{"MESSAGE": "raw-export-secret"}],
    })
    destination = tmp_path / "incident.json"
    assert diagnose.main([
        "--trace", identifier, "--journal-unit", "mochibot.service",
        "--export", str(destination),
    ]) == 0
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert report["records"][0]["spans"][0]["request"] == {"historical_input": "original-input"}
    assert "raw-export-secret" not in destination.read_text(encoding="utf-8")
    assert report["journal"]["events"][0]["MESSAGE"] == "[REDACTED]"
    before = destination.read_bytes()
    with pytest.raises(SystemExit) as blocked:
        diagnose.main(["--trace", identifier, "--export", str(destination)])
    assert blocked.value.code == 2
    assert destination.read_bytes() == before
    capsys.readouterr()


def test_missing_database_is_not_created(tmp_path, monkeypatch, capsys):
    import mochi.db as db

    missing = tmp_path / "missing.db"
    monkeypatch.setattr(db, "DB_PATH", missing)
    assert diagnose.main([]) == 1
    assert not missing.exists()
    assert "OperationalError" in capsys.readouterr().err


def test_export_refuses_when_credential_redaction_cannot_be_loaded(tmp_path, monkeypatch, capsys):
    from mochi.admin import admin_crypto

    conn = _connect()
    conn.execute(
        "INSERT INTO skill_config (skill_name,key,value,updated_at) "
        "VALUES ('test','key','gAAAAA-unreadable','2026-10-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(admin_crypto, "decrypt_api_key", lambda value: "")
    destination = tmp_path / "must-not-export.json"
    assert diagnose.main(["--export", str(destination)]) == 1
    assert not destination.exists()
    assert "Cannot load credential redactions" in capsys.readouterr().err


def test_journal_failure_and_limits_are_explicit(monkeypatch):
    start = "2026-10-01T04:00:00+00:00"
    end = "2026-10-01T04:01:00+00:00"
    runner = Mock(return_value=SimpleNamespace(
        returncode=1, stdout="", stderr="permission denied",
    ))
    monkeypatch.setattr(diagnose.subprocess, "run", runner)
    missing = diagnose.read_journal("mochibot.service", start, end)
    assert missing["status"] == "unavailable"
    assert missing["error"] == "permission denied"
    with pytest.raises(ValueError):
        diagnose.read_journal("--all", start, end)
    assert runner.call_count == 1
    runner.return_value = SimpleNamespace(
        returncode=0, stdout="\n".join(json.dumps({"MESSAGE": str(i)}) for i in range(301)),
        stderr="",
    )
    result = diagnose.read_journal("mochibot.service", start, end)
    assert result["truncated"] is True
    assert len(result["events"]) == 300
    assert result["events"][0]["MESSAGE"] == "1"


def test_time_filter_requires_timezone_and_rejects_reversed_window(capsys):
    with pytest.raises(SystemExit):
        diagnose.main(["--since", "2026-10-01T12:00:00"])
    assert diagnose.main([
        "--since", "2026-10-02T00:00:00Z", "--until", "2026-10-01T00:00:00Z",
    ]) == 1
    assert "must not be after" in capsys.readouterr().err
