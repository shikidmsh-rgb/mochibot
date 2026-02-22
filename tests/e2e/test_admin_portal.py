"""E2E test for the admin portal API.

Uses a threaded approach: uvicorn runs in the asyncio event loop,
HTTP test client runs in a separate thread to avoid deadlocking.
"""

import asyncio
import json
import urllib.request
import urllib.error
import sys
import os
import socket
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

BASE = "http://127.0.0.1:18080"


def get(path):
    r = urllib.request.urlopen(f"{BASE}{path}", timeout=10)
    return json.loads(r.read())


def req(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    rq = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    rq.add_header("Content-Type", "application/json")
    try:
        r = urllib.request.urlopen(rq, timeout=10)
        return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code
    except Exception as e:
        return {"error": str(e)}, 0


def wait_for_server():
    for _ in range(30):
        try:
            get("/api/status")
            return True
        except Exception:
            time.sleep(0.5)
    return False


def run_all_checks():
    """Run all E2E checks (called from a thread)."""
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  [PASS] {name}")
            passed += 1
        else:
            print(f"  [FAIL] {name} -- {detail}")
            failed += 1

    if not wait_for_server():
        print("ERROR: Server did not start")
        return 1

    # ── Page 0: Status ──
    print("=== /api/status ===")
    s = get("/api/status")
    check("has config_status", "config_status" in s)
    check("has integrations", "integrations" in s)
    check("has skills_count", "skills_count" in s)
    check("has heartbeat_state", "heartbeat_state" in s)

    # ── Page 1: Models CRUD ──
    print("=== Models CRUD ===")
    models = get("/api/models")
    check("list empty", models == [])

    r, code = req("POST", "/api/models", {
        "name": "test-model", "provider": "openai",
        "model": "gpt-4o-mini", "api_key": "sk-test123", "base_url": ""
    })
    check("create model", r.get("ok") is True)

    models = get("/api/models")
    check("list has one", len(models) == 1)
    check("api_key masked", models[0]["api_key"] == "***")
    check("api_key_set true", models[0]["api_key_set"] is True)

    r, code = req("POST", "/api/models", {
        "name": "test-model", "provider": "openai",
        "model": "gpt-4o", "api_key": "__KEEP__", "base_url": "https://x.com"
    })
    check("update __KEEP__", r.get("ok") is True)
    models = get("/api/models")
    check("model updated", models[0]["model"] == "gpt-4o")
    check("key preserved", models[0]["api_key_set"] is True)

    # ── Tier Assignment ──
    print("=== Tier Assignment ===")
    tiers = get("/api/tiers")
    check("tiers have chat", "chat" in tiers)
    check("initial source=env", tiers["chat"]["source"] == "env")

    r, _ = req("PUT", "/api/tiers/lite", {"model_name": "test-model"})
    check("assign tier", r.get("ok") is True)
    tiers = get("/api/tiers")
    check("tier source=db", tiers["lite"]["source"] == "db:test-model")

    r, _ = req("DELETE", "/api/tiers/lite")
    check("revert tier", r.get("ok") is True)
    tiers = get("/api/tiers")
    check("reverted to env", tiers["lite"]["source"] == "env")

    req("PUT", "/api/tiers/deep", {"model_name": "test-model"})
    r, code = req("DELETE", "/api/models/test-model")
    check("delete guard 409", code == 409, f"got {code}")

    req("DELETE", "/api/tiers/deep")
    req("DELETE", "/api/models/test-model")
    models = get("/api/models")
    check("cleanup done", len(models) == 0)

    # ── Page 2: Heartbeat Config ──
    print("=== Heartbeat Config ===")
    cfg = get("/api/heartbeat/config")
    check("has interval key", "HEARTBEAT_INTERVAL_MINUTES" in cfg)
    check("env_default=20", cfg["HEARTBEAT_INTERVAL_MINUTES"]["env_default"] == 20)
    check("no override", cfg["HEARTBEAT_INTERVAL_MINUTES"]["override"] is None)

    r, _ = req("PUT", "/api/heartbeat/config", {"HEARTBEAT_INTERVAL_MINUTES": "15"})
    check("set override ok", r.get("ok") is True)
    cfg = get("/api/heartbeat/config")
    check("override=15", cfg["HEARTBEAT_INTERVAL_MINUTES"]["override"] == "15")
    check("effective=15", cfg["HEARTBEAT_INTERVAL_MINUTES"]["effective"] == 15)

    r, _ = req("PUT", "/api/heartbeat/config", {"HEARTBEAT_INTERVAL_MINUTES": None})
    check("reset override ok", r.get("ok") is True)
    cfg = get("/api/heartbeat/config")
    check("override cleared", cfg["HEARTBEAT_INTERVAL_MINUTES"]["override"] is None)
    check("effective=20", cfg["HEARTBEAT_INTERVAL_MINUTES"]["effective"] == 20)

    state = get("/api/heartbeat/state")
    check("state endpoint", "state" in state)

    # ── Page 2b: Basic Config ──
    print("=== Basic Config ===")
    bcfg = get("/api/basic/config")
    check("has timezone key", "TIMEZONE_OFFSET_HOURS" in bcfg)
    check("has token key", "AI_CHAT_MAX_COMPLETION_TOKENS" in bcfg)
    check("has maintenance key", "MAINTENANCE_HOUR" in bcfg)
    check("timezone default=0", bcfg["TIMEZONE_OFFSET_HOURS"]["env_default"] == 0)

    r, _ = req("PUT", "/api/basic/config", {"MAINTENANCE_HOUR": "5"})
    check("set basic override", r.get("ok") is True)
    bcfg = get("/api/basic/config")
    check("basic override=5", bcfg["MAINTENANCE_HOUR"]["override"] == "5")
    check("basic effective=5", bcfg["MAINTENANCE_HOUR"]["effective"] == 5)

    r, _ = req("PUT", "/api/basic/config", {"MAINTENANCE_HOUR": None})
    check("reset basic override", r.get("ok") is True)
    bcfg = get("/api/basic/config")
    check("basic override cleared", bcfg["MAINTENANCE_HOUR"]["override"] is None)

    # ── Page 3: Skills ──
    print("=== Skills ===")
    skills = get("/api/skills")
    check(f"skills loaded ({len(skills)})", len(skills) >= 5)
    names = [s["name"] for s in skills]
    check("has memory", "memory" in names)
    check("has oura", "oura" in names)

    oura = next(s for s in skills if s["name"] == "oura")
    check("oura requires_config", "OURA_CLIENT_ID" in oura.get("requires_config", []))
    check("oura enabled", oura.get("enabled") is True)

    r, _ = req("PUT", "/api/skills/oura/enabled", {"enabled": False})
    check("disable skill", r.get("ok") is True)
    skills = get("/api/skills")
    oura = next(s for s in skills if s["name"] == "oura")
    check("oura disabled", oura.get("enabled") is False)

    r, _ = req("PUT", "/api/skills/oura/enabled", {"enabled": True})
    check("re-enable skill", r.get("ok") is True)

    # ── Frontend ──
    print("=== Frontend ===")
    r = urllib.request.urlopen(f"{BASE}/", timeout=5)
    html = r.read().decode()
    check("HTML serves", len(html) > 1000)
    check("has dango emoji", "\U0001f361" in html)
    check("has page-welcome", "page-welcome" in html)
    check("has page-models", "page-models" in html)
    check("has page-basic", "page-basic" in html)

    # ── Rate Limiter (unit check) ──
    print("=== Rate Limiter ===")
    from mochi.admin.admin_server import _test_timestamps, _check_test_rate, _TEST_RATE_LIMIT
    _test_timestamps.clear()
    for i in range(_TEST_RATE_LIMIT):
        _test_timestamps.append(time.monotonic())
    try:
        _check_test_rate()
        check("rate limit triggered", False, "should have raised")
    except Exception as e:
        check("rate limit triggered", "Rate limit" in str(e))
    _test_timestamps.clear()

    # ── Summary ──
    print()
    total = passed + failed
    if failed == 0:
        print(f"ALL {total} E2E TESTS PASSED")
    else:
        print(f"{failed}/{total} TESTS FAILED")
    return failed


async def main():
    from mochi.db import init_db
    init_db()

    from mochi import skills as skill_registry
    skill_registry.discover()

    from mochi.admin.admin_server import app
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=18080, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    loop = asyncio.get_event_loop()
    failures = await loop.run_in_executor(None, run_all_checks)

    server.should_exit = True
    await serve_task
    return failures


if __name__ == "__main__":
    failures = asyncio.run(main())
    sys.exit(1 if failures else 0)
