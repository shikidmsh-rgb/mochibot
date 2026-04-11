"""MochiBot Admin Portal — FastAPI server.

Provides a web-based setup & configuration portal for MochiBot.
All endpoints are under /api, with optional token auth via ADMIN_TOKEN.
"""

import asyncio
import atexit
import collections
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, HTTPException, Request, Depends
    from fastapi.responses import HTMLResponse, JSONResponse
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PROMPTS_DIR = _PROJECT_ROOT / "prompts"

# ── Bot subprocess management ────────────────────────────────────────────
_bot_process: subprocess.Popen | None = None
_bot_log_lines: collections.deque = collections.deque(maxlen=500)
_bot_lock = threading.Lock()


def _bot_reader_thread(proc: subprocess.Popen):
    """Read bot subprocess stdout line by line into the log buffer."""
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
            _bot_log_lines.append(line)
    except Exception:
        pass


def _kill_bot():
    """Terminate the bot subprocess if running."""
    global _bot_process
    with _bot_lock:
        if _bot_process and _bot_process.poll() is None:
            _bot_process.terminate()
            try:
                _bot_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _bot_process.kill()
            _bot_process = None


atexit.register(_kill_bot)

# Windows uses SIGBREAK for Ctrl+C in console; Unix uses SIGTERM
for _sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
    if _sig is not None:
        try:
            signal.signal(_sig, lambda s, f: (_kill_bot(), sys.exit(0)))
        except (OSError, ValueError):
            pass  # can't set handler in non-main thread

_PROMPT_META: dict[str, dict[str, str]] = {
    "system_chat/soul.md": {
        "label": "灵魂 Soul",
        "desc": "Agent 的核心人格：说话风格、语气、性格特点",
    },
    "system_chat/agent.md": {
        "label": "能力 Agent",
        "desc": "Agent 能做什么、怎么做：技能、工具、行为规则",
    },
    "system_chat/user.md": {
        "label": "用户画像 User",
        "desc": "主人的信息：称呼、喜好，让 Agent 更了解你",
    },
    "system_chat/runtime_context.md": {
        "label": "运行时上下文 Runtime",
        "desc": "每次对话自动注入的动态信息（时间、状态等）",
    },
    "think_system.md": {
        "label": "思考框架 Think",
        "desc": "Agent 内部思考时使用的 system prompt",
    },
    "memory_extract.md": {
        "label": "记忆提取 Memory",
        "desc": "从对话中提取长期记忆的指令模板",
    },
}

_ALLOWED_PROMPTS: frozenset[str] = frozenset(_PROMPT_META.keys())

_MAX_PROMPT_SIZE = 50 * 1024  # 50 KB


def _prompt_path(name: str) -> Path:
    """Resolve prompt name to safe absolute path inside prompts directory."""
    normalized = PurePosixPath(name)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError("Invalid prompt path")
    return _PROMPTS_DIR / Path(*normalized.parts)


if HAS_FASTAPI:
    app = FastAPI(title="MochiBot Setup Portal", docs_url="/api/docs")

    @app.on_event("startup")
    async def _startup():
        """Ensure DB and registries are initialized (needed for uvicorn --reload)."""
        from mochi.db import init_db
        from mochi import skills as skill_registry
        from mochi import observers as observer_registry
        init_db()
        skill_registry.discover()
        observer_registry.discover()

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

    def _embedding_integration_status(provider: str, _int_status) -> dict:
        """Dynamic integration status based on EMBEDDING_PROVIDER."""
        p = (provider or "").strip().lower()
        if p == "none":
            return {"configured": False, "missing": [], "disabled": True}
        if p == "openai":
            return _int_status("embedding", ["EMBEDDING_API_KEY"])
        if p == "azure_openai":
            return _int_status("embedding", ["EMBEDDING_API_KEY", "EMBEDDING_BASE_URL"])
        if p == "ollama":
            return _int_status("embedding", ["EMBEDDING_MODEL"])
        # Legacy auto-detect (EMBEDDING_PROVIDER not set)
        return _int_status("embedding", ["AZURE_EMBEDDING_ENDPOINT", "AZURE_EMBEDDING_API_KEY"])

    @app.get("/api/status", dependencies=[Depends(_verify_token)])
    async def get_status():
        from mochi.config import (
            CHAT_MODEL, CHAT_API_KEY, CHAT_PROVIDER,
            TELEGRAM_BOT_TOKEN,
            OWNER_USER_ID, TIER_ROUTING_ENABLED,
            OURA_CLIENT_ID, OURA_CLIENT_SECRET, OURA_REFRESH_TOKEN,
            EMBEDDING_PROVIDER,
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
            "owner_user_id": {"set": bool(OWNER_USER_ID), "value": str(OWNER_USER_ID) if OWNER_USER_ID else ""},
            "tier_routing_enabled": {"set": TIER_ROUTING_ENABLED, "value": str(TIER_ROUTING_ENABLED)},
        }

        integrations = {
            "weather": _integration_status("weather", ["WEATHER_CITY"]),
            "oura": _integration_status("oura", ["OURA_CLIENT_ID", "OURA_CLIENT_SECRET", "OURA_REFRESH_TOKEN"]),
            "web_search": {"configured": True, "missing": [], "note": "DuckDuckGo — no API key needed"},
            "embedding": _embedding_integration_status(EMBEDDING_PROVIDER, _integration_status),
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
    # Bot process management
    # ═══════════════════════════════════════════════════════════════════════

    @app.post("/api/bot/start", dependencies=[Depends(_verify_token)])
    async def api_bot_start():
        global _bot_process
        with _bot_lock:
            if _bot_process and _bot_process.poll() is None:
                raise HTTPException(409, "Bot is already running")
            env = {**os.environ, "ADMIN_ENABLED": "false"}
            _bot_log_lines.clear()
            _bot_process = subprocess.Popen(
                [sys.executable, "-u", "-m", "mochi.main"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(_PROJECT_ROOT),
            )
            t = threading.Thread(
                target=_bot_reader_thread, args=(_bot_process,), daemon=True,
            )
            t.start()
        return {"ok": True, "pid": _bot_process.pid}

    @app.post("/api/bot/stop", dependencies=[Depends(_verify_token)])
    async def api_bot_stop():
        global _bot_process
        with _bot_lock:
            if not _bot_process or _bot_process.poll() is not None:
                _bot_process = None
                raise HTTPException(409, "Bot is not running")
            _bot_process.terminate()
            try:
                _bot_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _bot_process.kill()
            _bot_process = None
        return {"ok": True}

    @app.get("/api/bot/status", dependencies=[Depends(_verify_token)])
    async def api_bot_status():
        with _bot_lock:
            if _bot_process is None:
                return {"running": False, "pid": None, "lines": list(_bot_log_lines)}
            rc = _bot_process.poll()
            if rc is not None:
                return {
                    "running": False, "pid": None,
                    "exit_code": rc, "lines": list(_bot_log_lines),
                }
            return {
                "running": True, "pid": _bot_process.pid,
                "lines": list(_bot_log_lines),
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
        "MAINTENANCE_HOUR": "int",
        "MAINTENANCE_ENABLED": "bool",
        # Token limits
        "AI_CHAT_MAX_COMPLETION_TOKENS": "int",
        "TOOL_LOOP_MAX_ROUNDS": "int",
        "TOOL_LOOP_PER_TOOL_LIMIT": "int",
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
    # Observers (displayed on Heartbeat page)
    # ═══════════════════════════════════════════════════════════════════════

    @app.get("/api/observers", dependencies=[Depends(_verify_token)])
    async def api_list_observers():
        from mochi.observers import get_observers_for_admin
        return get_observers_for_admin()

    @app.put("/api/observers/{name}/config", dependencies=[Depends(_verify_token)])
    async def api_set_observer_config(name: str, request: Request):
        """Set observer config overrides (e.g. interval)."""
        from mochi.observers import get_observer
        from mochi.db import set_skill_config, delete_skill_config

        obs = get_observer(name)
        if not obs:
            return {"ok": False, "error": f"Unknown observer: {name}"}

        body = await request.json()
        updated = []
        errors = []

        for key, value in body.items():
            if key == "interval":
                if value is None:
                    # Reset to default
                    delete_skill_config(f"_observer:{name}", "interval")
                    updated.append("interval")
                else:
                    try:
                        iv = int(value)
                        if iv < 1 or iv > 1440:
                            errors.append({"key": key, "error": "interval must be 1-1440"})
                            continue
                        set_skill_config(f"_observer:{name}", "interval", str(iv))
                        updated.append("interval")
                    except (ValueError, TypeError):
                        errors.append({"key": key, "error": "interval must be an integer"})
            else:
                errors.append({"key": key, "error": f"Unknown observer config key: {key}"})

        return {"ok": len(errors) == 0, "updated": updated, "errors": errors}

    # ═══════════════════════════════════════════════════════════════════════
    # Page 3: Skills
    # ═══════════════════════════════════════════════════════════════════════

    @app.get("/api/skills", dependencies=[Depends(_verify_token)])
    async def api_list_skills():
        from mochi.skills import get_skill_info_all
        from mochi.observers import get_observer_info_all
        return get_skill_info_all() + get_observer_info_all()

    @app.put("/api/skills/{name}/enabled", dependencies=[Depends(_verify_token)])
    async def api_set_skill_enabled(name: str, request: Request):
        body = await request.json()
        enabled = body.get("enabled", True)
        from mochi.db import set_skill_enabled
        set_skill_enabled(name, enabled)
        return {"ok": True, "skill": name, "enabled": enabled}

    @app.put("/api/skills/{name}/config", dependencies=[Depends(_verify_token)])
    async def api_set_skill_config(name: str, request: Request):
        """Set config values for a skill. Stored in DB (not .env).

        Also updates os.environ so changes take effect immediately.
        """
        body = await request.json()
        from mochi.skills import get_skill
        from mochi.db import set_skill_config

        skill = get_skill(name)
        if not skill:
            return {"ok": False, "error": f"Unknown skill: {name}"}

        # Validate keys against skill's declared config
        allowed_keys = set(skill.requires_config)
        for entry in skill.config_schema:
            allowed_keys.add(entry["key"])
        for field in skill._config_schema_typed:
            allowed_keys.add(field.key)

        written = []
        errors = []
        for key, value in body.items():
            if key not in allowed_keys:
                errors.append({"key": key, "error": f"Key not declared by skill {name}"})
                continue
            set_skill_config(name, key, str(value))
            os.environ[key] = str(value)  # immediate effect
            written.append(key)

        # Hot-reload resolved config
        if written:
            skill.refresh_config()

        return {"ok": len(errors) == 0, "written": written, "errors": errors}

    @app.get("/api/skills/{name}/config", dependencies=[Depends(_verify_token)])
    async def api_get_skill_config(name: str):
        """Get config values for a skill. Merges DB + env + schema defaults.

        Secret values are masked in the response.
        """
        from mochi.skills import get_skill
        from mochi.db import get_skill_config

        skill = get_skill(name)
        if not skill:
            return {"ok": False, "error": f"Unknown skill: {name}"}

        db_config = get_skill_config(name)
        secret_keys = {e["key"] for e in skill.config_schema if e.get("secret")}

        config = []
        for entry in skill.config_schema:
            key = entry["key"]
            # Priority: DB > env > schema default
            if key in db_config:
                raw_value = db_config[key]
            elif os.getenv(key):
                raw_value = os.getenv(key, "")
            else:
                raw_value = entry.get("default", "")

            is_set = bool(raw_value)
            display_value = "***" if (key in secret_keys and is_set) else raw_value

            config.append({
                "key": key,
                "value": display_value,
                "is_set": is_set,
                "source": "db" if key in db_config else ("env" if os.getenv(key) else "default"),
                "secret": key in secret_keys,
                "type": entry.get("type", "string"),
                "description": entry.get("description", ""),
            })

        # Also include requires_config keys not in schema
        schema_keys = {e["key"] for e in skill.config_schema}
        for key in skill.requires_config:
            if key in schema_keys:
                continue
            raw_value = db_config.get(key, os.getenv(key, ""))
            config.append({
                "key": key,
                "value": "***" if raw_value else "",
                "is_set": bool(raw_value),
                "source": "db" if key in db_config else ("env" if os.getenv(key) else ""),
                "secret": True,  # assume secret if not in schema
                "type": "string",
                "description": "",
            })

        return {"ok": True, "skill": name, "config": config}

    # ── Generic .env writer ───────────────────────────────────────────────

    @app.put("/api/env", dependencies=[Depends(_verify_token)])
    async def api_write_env(request: Request):
        """Write key=value pairs to .env (whitelist enforced)."""
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

    # ═══════════════════════════════════════════════════════════════════════
    # Page 4: Prompts
    # ═══════════════════════════════════════════════════════════════════════

    @app.get("/api/prompts", dependencies=[Depends(_verify_token)])
    async def api_list_prompts():
        """List all editable prompt files with char counts."""
        prompts = []
        for fname in sorted(_ALLOWED_PROMPTS):
            meta = _PROMPT_META.get(fname, {})
            p = _prompt_path(fname)
            if p.exists():
                content = p.read_text(encoding="utf-8")
                prompts.append({"name": fname, "label": meta.get("label", fname),
                                "desc": meta.get("desc", ""), "chars": len(content), "exists": True})
            else:
                prompts.append({"name": fname, "label": meta.get("label", fname),
                                "desc": meta.get("desc", ""), "chars": 0, "exists": False})
        return {"prompts": prompts}

    @app.get("/api/prompts/{name:path}", dependencies=[Depends(_verify_token)])
    async def api_get_prompt(name: str):
        """Read a single prompt file."""
        if name not in _ALLOWED_PROMPTS:
            raise HTTPException(403, f"'{name}' not in allowed list")
        p = _prompt_path(name)
        if not p.exists():
            raise HTTPException(404, "Prompt file not found")
        return {"name": name, "content": p.read_text(encoding="utf-8")}

    @app.post("/api/prompts/{name:path}", dependencies=[Depends(_verify_token)])
    async def api_save_prompt(name: str, request: Request):
        """Save content to a prompt file. Hot-reloads immediately."""
        if name not in _ALLOWED_PROMPTS:
            raise HTTPException(403, f"'{name}' not in allowed list")
        body = await request.json()
        content = body.get("content", "")
        if len(content.encode("utf-8")) > _MAX_PROMPT_SIZE:
            raise HTTPException(413, f"Prompt too large (max {_MAX_PROMPT_SIZE // 1024}KB)")
        p = _prompt_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # Hot-reload
        from mochi.prompt_loader import reload_all
        reload_all()
        log.info("Admin: saved prompt '%s' (%d chars)", name, len(content))
        return {"ok": True, "name": name, "chars": len(content)}


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
