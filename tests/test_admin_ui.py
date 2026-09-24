from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


@pytest.mark.asyncio
async def test_admin_update_hands_off_only_after_authenticated_response(monkeypatch):
    import httpx
    import mochi.config as config
    import mochi.update_service as updates
    from mochi.admin.admin_server import app

    monkeypatch.setattr(config, "ADMIN_TOKEN", "test-admin-token")
    release = SimpleNamespace(
        available=True, tag="v1.0.12", version="1.0.12",
        current_version="1.0.1", notes="Release notes",
    )
    check = AsyncMock(return_value=release)
    monkeypatch.setattr(updates, "check_for_update", check)
    monkeypatch.setattr(updates, "prepare_update", AsyncMock(return_value={}))
    monkeypatch.setattr(updates, "stage_update", lambda *args, **kwargs: {"request_id": "update"})
    events = []
    monkeypatch.setattr(updates, "request_update_exit", lambda _: events.append("exit"))

    async def application(scope, receive, send):
        async def capture(message):
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                events.append("response")
        await app(scope, receive, capture)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        denied = await client.post("/api/system/update-apply")
        assert denied.status_code == 403
        check.assert_not_awaited()
        events.clear()
        response = await client.post(
            "/api/system/update-apply", headers={"Authorization": "Bearer test-admin-token"},
        )
    assert response.json()["prepared"] is True
    assert events == ["response", "exit"]


def test_launcher_separates_restart_from_update_and_opens_browser_once(monkeypatch):
    import runpy
    import subprocess
    import sys
    import time

    launcher = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "start.py"))
    monkeypatch.setattr(sys, "argv", ["start.py", "--open-browser"])
    monkeypatch.setattr(time, "sleep", lambda _: None)
    exits = iter([42, 44, 0, 0])
    calls = []

    def run(command, *, env):
        assert env["MOCHIBOT_UPDATE_LAUNCHER"] == "1"
        calls.append(command)
        return SimpleNamespace(returncode=next(exits))

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit) as finished:
        launcher["main"]()
    assert finished.value.code == 0
    assert [command[2] for command in calls] == [
        "mochi.main", "mochi.main", "mochi.update_service", "mochi.main",
    ]
    assert "--open-browser" in calls[0]
    assert all("--open-browser" not in command for command in calls[1:])


def test_early_update_exit_request_is_not_lost(monkeypatch):
    import mochi.shutdown as shutdown

    monkeypatch.setattr(shutdown, "_restart_event", None)
    monkeypatch.setattr(shutdown, "_requested_exit_code", None)
    shutdown.request_process_exit(shutdown.UPDATE_EXIT_CODE)
    assert shutdown.init_restart_event().is_set()
    assert shutdown.requested_exit_code() == 44


def test_setup_keeps_admin_local_and_preserves_existing_token(monkeypatch):
    import sys
    import uvicorn
    import mochi.config as config
    from mochi.admin import __main__ as entry, admin_db, admin_env

    monkeypatch.setattr(config, "WEIXIN_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_BIND", "127.0.0.1")
    monkeypatch.setattr(config, "ADMIN_TOKEN", "existing-token")
    monkeypatch.setattr(admin_db, "are_required_tiers_ready", lambda: False)
    assert config.validate_config() == "setup_mode"
    assert config.ADMIN_BIND == "127.0.0.1"
    assert config.ADMIN_TOKEN == "existing-token"

    monkeypatch.setattr(sys, "argv", ["admin", "--no-browser"])
    captured = []

    def server(cfg):
        captured.append(cfg.host)
        return SimpleNamespace(serve=AsyncMock())

    monkeypatch.setattr(uvicorn, "Server", server)
    entry.main()
    assert captured == ["127.0.0.1"]

    monkeypatch.setattr(admin_env, "read_env_value", lambda key: "saved-token")
    assert entry._ensure_admin_token(entry.logging.getLogger(__name__)) == "existing-token"
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setenv("ADMIN_TOKEN", "")
    assert entry._ensure_admin_token(entry.logging.getLogger(__name__)) == "saved-token"
    assert config.ADMIN_TOKEN == "saved-token"
    monkeypatch.setattr(sys, "argv", ["admin", "--no-browser", "--bind", "0.0.0.0"])
    entry.main()
    assert captured == ["127.0.0.1", "0.0.0.0"]


def test_memory_evidence_receipts_stay_owner_scoped(monkeypatch):
    from fastapi.testclient import TestClient

    from mochi.admin.admin_server import app
    import mochi.config as config
    from mochi.db import insert_memory_item, save_message

    monkeypatch.setattr(config, "ADMIN_TOKEN", "test-admin-token")

    source_text = "<img src=x onerror=alert(1)>" + ("x" * 2100)
    source_id = save_message(1, "user", source_text)
    other_owner_id = save_message(2, "user", "private other-owner text")
    assistant_id = save_message(1, "assistant", "assistant is not evidence")
    missing_id = assistant_id + 1000
    item_id = insert_memory_item(
        1,
        "喜欢安全可解释的记忆",
        2,
        source="lite_extracted",
        evidence_message_ids=[
            source_id, other_owner_id, assistant_id, missing_id,
        ],
    )
    legacy_id = insert_memory_item(
        1,
        "一条没有原始对话来源的旧记忆",
        1,
        source="import",
    )
    other_owner_item_id = insert_memory_item(
        2,
        "其他 owner 的记忆",
        3,
        source="lite_extracted",
        evidence_message_ids=[other_owner_id],
    )

    client = TestClient(
        app,
        headers={"Authorization": "Bearer test-admin-token"},
    )
    response = client.get(f"/api/memory-items/{item_id}/evidence")
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["source_status"] == "recorded"
    assert receipt["item"]["content"] == "喜欢安全可解释的记忆"
    assert receipt["item"]["importance"] == 2
    assert receipt["item"]["source"] == "lite_extracted"
    assert receipt["item"]["created_at"]
    assert receipt["item"]["updated_at"]
    assert receipt["source_messages"][0] == {
        "message_id": source_id,
        "available": True,
        "created_at": receipt["source_messages"][0]["created_at"],
        "content": source_text[:2000],
        "truncated": True,
    }
    assert receipt["source_messages"][1:] == [
        {"message_id": other_owner_id, "available": False},
        {"message_id": assistant_id, "available": False},
        {"message_id": missing_id, "available": False},
    ]
    assert "private other-owner text" not in response.text
    assert "assistant is not evidence" not in response.text

    legacy = client.get(f"/api/memory-items/{legacy_id}/evidence").json()
    assert legacy["source_status"] == "not_recorded"
    assert legacy["source_messages"] == []
    assert client.get(
        f"/api/memory-items/{other_owner_item_id}/evidence"
    ).status_code == 404

    listing = client.get("/api/memory-items").json()
    assert "source_messages" not in listing["items"][0]
    assert source_text not in str(listing)
