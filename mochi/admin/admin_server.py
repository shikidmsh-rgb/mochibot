"""MochiBot Admin Portal — FastAPI server.

Provides a web-based setup & configuration portal for MochiBot.
All endpoints are under /api, with optional token auth via ADMIN_TOKEN.
"""

import asyncio
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, HTTPException, Request, Depends
    from fastapi.responses import HTMLResponse, JSONResponse
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

if HAS_FASTAPI:
    app = FastAPI(title="MochiBot Setup Portal", docs_url="/api/docs")

    # ── Auth ──────────────────────────────────────────────────────────────

    async def _verify_token(request: Request):
        """Optional token auth. If ADMIN_TOKEN is set, require it."""
        from mochi.config import ADMIN_TOKEN
        if not ADMIN_TOKEN:
            return
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token:
            token = request.query_params.get("token", "")
        if token != ADMIN_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid admin token")

    # ── Rate limiter for connection test ──────────────────────────────────

    _test_timestamps: list[float] = []
    _TEST_RATE_LIMIT = 5        # max calls
    _TEST_RATE_WINDOW = 60.0    # per N seconds

    def _check_test_rate():
        """Rate-limit connection test to prevent API credit abuse."""
        now = time.monotonic()
        _test_timestamps[:] = [t for t in _test_timestamps if now - t < _TEST_RATE_WINDOW]
        if len(_test_timestamps) >= _TEST_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit: max {_TEST_RATE_LIMIT} tests per {int(_TEST_RATE_WINDOW)}s"
            )
        _test_timestamps.append(now)

    # ── Frontend ──────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def serve_frontend():
        html_path = Path(__file__).parent / "index.html"
        if not html_path.exists():
            raise HTTPException(status_code=404, detail="Frontend not found")
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    # ═══════════════════════════════════════════════════════════════════════
    # Page 0: Status
    # ═══════════════════════════════════════════════════════════════════════

    @app.get("/api/status", dependencies=[Depends(_verify_token)])
    async def get_status():
        from mochi.config import (
            CHAT_MODEL, CHAT_API_KEY, CHAT_PROVIDER,
            TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN,
            OWNER_USER_ID, TIER_ROUTING_ENABLED,
            OURA_CLIENT_ID, OURA_CLIENT_SECRET, OURA_REFRESH_TOKEN,
            TAVILY_API_KEY,
            AZURE_EMBEDDING_ENDPOINT, AZURE_EMBEDDING_API_KEY,
            DB_PATH,
        )
        from mochi.admin.admin_env import env_key_is_set

        def _integration_status(name: str, keys: list[str]) -> dict:
            missing = [k for k in keys if not env_key_is_set(k)]
            return {"configured": len(missing) == 0, "missing": missing}

        config_status = {
            "chat_model": {"set": bool(CHAT_MODEL), "value": CHAT_MODEL or ""},
            "chat_api_key": {"set": bool(CHAT_API_KEY)},
            "chat_provider": {"set": bool(CHAT_PROVIDER), "value": CHAT_PROVIDER},
            "telegram_bot_token": {"set": bool(TELEGRAM_BOT_TOKEN)},
            "discord_bot_token": {"set": bool(DISCORD_BOT_TOKEN)},
            "owner_user_id": {"set": bool(OWNER_USER_ID), "value": str(OWNER_USER_ID) if OWNER_USER_ID else ""},
            "tier_routing_enabled": {"set": TIER_ROUTING_ENABLED, "value": str(TIER_ROUTING_ENABLED)},
        }

        integrations = {
            "weather": _integration_status("weather", ["OPENWEATHER_API_KEY", "WEATHER_LAT", "WEATHER_LON"]),
            "oura": _integration_status("oura", ["OURA_CLIENT_ID", "OURA_CLIENT_SECRET", "OURA_REFRESH_TOKEN"]),
            "web_search": _integration_status("web_search", ["TAVILY_API_KEY"]),
            "embedding": _integration_status("embedding", ["AZURE_EMBEDDING_ENDPOINT", "AZURE_EMBEDDING_API_KEY"]),
        }

        # Skill stats
        try:
            from mochi.skills import get_skill_info_all
            skills = get_skill_info_all()
            skills_count = len(skills)
            skills_disabled = sum(1 for s in skills if not s.get("enabled", True))
        except Exception:
            skills_count = 0
            skills_disabled = 0

        # Heartbeat state
        try:
            from mochi.heartbeat import get_stats
            hb = get_stats()
        except Exception:
            hb = {"state": "UNKNOWN"}

        return {
            "first_run": not CHAT_MODEL or not CHAT_API_KEY,
            "config_status": config_status,
            "integrations": integrations,
            "skills_count": skills_count,
            "skills_disabled": skills_disabled,
            "heartbeat_state": hb.get("state", "UNKNOWN"),
            "db_path": str(DB_PATH),
        }

    # ═══════════════════════════════════════════════════════════════════════
    # Page 1: Models
    # ═══════════════════════════════════════════════════════════════════════

    @app.get("/api/models", dependencies=[Depends(_verify_token)])
    async def api_list_models():
        from mochi.admin.admin_db import list_models
        return list_models(mask_keys=True)

    @app.post("/api/models", dependencies=[Depends(_verify_token)])
    async def api_upsert_model(request: Request):
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            raise HTTPException(400, "name is required")
        from mochi.admin.admin_db import upsert_model
        try:
            upsert_model(
                name=name,
                provider=body.get("provider", "openai"),
                model=body.get("model", ""),
                api_key=body.get("api_key", ""),
                base_url=body.get("base_url", ""),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.delete("/api/models/{name}", dependencies=[Depends(_verify_token)])
    async def api_delete_model(name: str):
        from mochi.admin.admin_db import delete_model
        try:
            deleted = delete_model(name)
        except ValueError as e:
            raise HTTPException(409, str(e))
        if not deleted:
            raise HTTPException(404, f"Model '{name}' not found")
        return {"ok": True}

    @app.post("/api/models/{name}/test", dependencies=[Depends(_verify_token)])
    async def api_test_model(name: str):
        _check_test_rate()
        from mochi.admin.admin_db import get_model
        from mochi.llm import _make_client
        entry = get_model(name, mask_key=False)
        if not entry:
            raise HTTPException(404, f"Model '{name}' not found")

        try:
            client = _make_client(
                entry["provider"], entry["api_key"],
                entry["model"], entry["base_url"],
            )
            start = time.monotonic()
            resp = await asyncio.to_thread(
                client.chat,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5,
            )
            elapsed = int((time.monotonic() - start) * 1000)
            return {"ok": True, "model": resp.model, "latency_ms": elapsed}
        except Exception as e:
            err_str = str(e)
            # o-series models reject max_tokens — treat as connected
            if "max_tokens" in err_str.lower() or "max_completion_tokens" in err_str.lower():
                return {"ok": True, "model": entry["model"], "note": "Connected (reasoning model)"}
            return {"ok": False, "error": err_str[:500]}

    # ── Tiers ─────────────────────────────────────────────────────────────

    @app.get("/api/tiers", dependencies=[Depends(_verify_token)])
    async def api_get_tiers():
        from mochi.admin.admin_db import get_tier_effective_config
        config = get_tier_effective_config()
        # Mask api_key
        for tier, cfg in config.items():
            cfg.pop("api_key", None)
        return config

    @app.put("/api/tiers/{tier}", dependencies=[Depends(_verify_token)])
    async def api_set_tier(tier: str, request: Request):
        body = await request.json()
        model_name = body.get("model_name", "").strip()
        if not model_name:
            raise HTTPException(400, "model_name is required")
        from mochi.admin.admin_db import set_tier_assignment, get_model
        try:
            set_tier_assignment(tier, model_name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Hot-reload
        try:
            entry = get_model(model_name, mask_key=False)
            if entry:
                from mochi.model_pool import get_pool
                get_pool().reload_tier(
                    tier, entry["provider"], entry["api_key"],
                    entry["model"], entry["base_url"],
                )
        except Exception as e:
            log.warning("Tier hot-reload failed for '%s': %s", tier, e)
        return {"ok": True}

    @app.delete("/api/tiers/{tier}", dependencies=[Depends(_verify_token)])
    async def api_clear_tier(tier: str):
        from mochi.admin.admin_db import clear_tier_assignment
        try:
            clear_tier_assignment(tier)
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Reload from .env
        try:
            from mochi.model_pool import get_pool
            pool = get_pool()
            provider, api_key, model, base_url = pool.get_tier_env_config(tier)
            pool.reload_tier(tier, provider, api_key, model, base_url)
        except Exception as e:
            log.warning("Tier revert failed for '%s': %s", tier, e)
        return {"ok": True}

    # ═══════════════════════════════════════════════════════════════════════
    # Page 2: Heartbeat Config
    # ═══════════════════════════════════════════════════════════════════════

    _HEARTBEAT_PARAMS = {
        "HEARTBEAT_INTERVAL_MINUTES": "int",
        "AWAKE_HOUR_START": "int",
        "AWAKE_HOUR_END": "int",
        "FORCE_SLEEP_HOUR": "int",
        "FORCE_WAKE_HOUR": "int",
        "MAX_DAILY_PROACTIVE": "int",
        "PROACTIVE_COOLDOWN_SECONDS": "int",
        "THINK_FALLBACK_MINUTES": "int",
        "LLM_HEARTBEAT_TIMEOUT_SECONDS": "int",
        "MORNING_REPORT_HOUR": "int",
        "EVENING_REPORT_HOUR": "int",
        "MAINTENANCE_HOUR": "int",
        "MAINTENANCE_ENABLED": "bool",
    }

    @app.get("/api/heartbeat/config", dependencies=[Depends(_verify_token)])
    async def api_get_heartbeat_config():
        import mochi.config as cfg
        from mochi.admin.admin_db import get_system_overrides
        overrides = get_system_overrides()
        result = {}
        for key, typ in _HEARTBEAT_PARAMS.items():
            env_val = getattr(cfg, key, None)
            override_val = overrides.get(key)
            result[key] = {
                "env_default": env_val,
                "override": override_val,
                "effective": _cast(override_val, typ) if override_val is not None else env_val,
                "type": typ,
            }
        return result

    @app.put("/api/heartbeat/config", dependencies=[Depends(_verify_token)])
    async def api_set_heartbeat_config(request: Request):
        body = await request.json()
        from mochi.admin.admin_db import set_system_override, clear_system_override
        updated = []
        for key, value in body.items():
            if key not in _HEARTBEAT_PARAMS:
                continue
            if value is None:
                clear_system_override(key)
            else:
                set_system_override(key, str(value))
            updated.append(key)
        return {"ok": True, "updated": updated}

    @app.get("/api/heartbeat/state", dependencies=[Depends(_verify_token)])
    async def api_get_heartbeat_state():
        try:
            from mochi.heartbeat import get_stats
            return get_stats()
        except Exception as e:
            return {"state": "UNKNOWN", "error": str(e)}

    def _cast(value: str, typ: str):
        if typ == "bool":
            return value.lower() in ("true", "1", "yes")
        if typ == "int":
            try:
                return int(value)
            except (ValueError, TypeError):
                return value
        return value

    # ═══════════════════════════════════════════════════════════════════════
    # Page 3: Skills
    # ═══════════════════════════════════════════════════════════════════════

    @app.get("/api/skills", dependencies=[Depends(_verify_token)])
    async def api_list_skills():
        from mochi.skills import get_skill_info_all
        return get_skill_info_all()

    @app.put("/api/skills/{name}/enabled", dependencies=[Depends(_verify_token)])
    async def api_set_skill_enabled(name: str, request: Request):
        body = await request.json()
        enabled = body.get("enabled", True)
        from mochi.db import set_skill_enabled
        set_skill_enabled(name, enabled)
        return {"ok": True, "skill": name, "enabled": enabled}

    @app.put("/api/skills/{name}/config", dependencies=[Depends(_verify_token)])
    async def api_set_skill_config(name: str, request: Request):
        body = await request.json()
        from mochi.admin.admin_env import write_env_value
        written = []
        errors = []
        for key, value in body.items():
            try:
                write_env_value(key, str(value))
                written.append(key)
            except (ValueError, PermissionError) as e:
                errors.append({"key": key, "error": str(e)})
        return {"ok": len(errors) == 0, "written": written, "errors": errors}


# ═══════════════════════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════════════════════

async def start_admin_server(port: int = 8080, bind: str = "127.0.0.1"):
    """Start the admin portal as an async task."""
    if not HAS_FASTAPI:
        raise ImportError("fastapi/uvicorn not installed")

    import uvicorn
    config = uvicorn.Config(
        app, host=bind, port=port,
        log_level="warning",  # don't spam bot logs with HTTP access logs
    )
    server = uvicorn.Server(config)
    await server.serve()
