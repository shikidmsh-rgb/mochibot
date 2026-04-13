"""MochiBot Admin Portal — FastAPI server.

Provides a web-based setup & configuration portal for MochiBot.
All endpoints are under /api, with optional token auth via ADMIN_TOKEN.
"""

import asyncio
import atexit
import collections
import hmac
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
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response as StarletteResponse
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


def _start_bot_process():
    """Start (or restart) the bot subprocess. Returns the new PID."""
    global _bot_process
    _kill_orphaned_bots()
    with _bot_lock:
        if _bot_process and _bot_process.poll() is None:
            _bot_process.terminate()
            try:
                _bot_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _bot_process.kill()
            _bot_process = None
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
    return _bot_process.pid


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


def _kill_orphaned_bots():
    """Kill any orphaned mochi.main processes from previous admin sessions.

    When the admin portal restarts, it loses the _bot_process reference but
    old bot subprocesses may still be running and holding the Telegram
    long-polling connection, preventing the new bot from receiving updates.
    """
    my_pid = os.getpid()
    try:
        if sys.platform == "win32":
            # WMIC lists PIDs whose command line contains "mochi.main"
            out = subprocess.check_output(
                ["wmic", "process", "where",
                 "CommandLine like '%mochi.main%' and not CommandLine like '%wmic%'",
                 "get", "ProcessId"],
                text=True, stderr=subprocess.DEVNULL,
            )
            for line in out.strip().splitlines()[1:]:
                line = line.strip()
                if line.isdigit():
                    pid = int(line)
                    if pid != my_pid:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except OSError:
                            pass
        else:
            out = subprocess.check_output(
                ["pgrep", "-f", "mochi.main"], text=True, stderr=subprocess.DEVNULL,
            )
            for line in out.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    pid = int(line)
                    if pid != my_pid:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except OSError:
                            pass
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass  # no matching processes or command not available


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

    # ── CSRF Origin checking middleware ──────────────────────────────────

    _SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
    _LOCALHOST_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

    class _CSRFMiddleware(BaseHTTPMiddleware):
        """Block cross-origin state-changing requests.

        Requests with a valid Bearer token skip this check (API/cURL use).
        """

        async def dispatch(self, request: Request, call_next):
            if request.method in _SAFE_METHODS:
                return await call_next(request)

            # If the caller provides a Bearer token, they are an API client
            # (cURL, Postman, programmatic), not a CSRF victim's browser.
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and auth.removeprefix("Bearer ").strip():
                return await call_next(request)

            # Check Origin (preferred) or Referer header
            origin = request.headers.get("Origin", "")
            if not origin:
                referer = request.headers.get("Referer", "")
                if referer:
                    from urllib.parse import urlparse
                    ref = urlparse(referer)
                    origin = f"{ref.scheme}://{ref.hostname}" if ref.hostname else ""
                    if ref.port:
                        origin += f":{ref.port}"

            if not origin:
                return StarletteResponse(
                    content="Missing Origin header",
                    status_code=403,
                )

            from urllib.parse import urlparse
            parsed = urlparse(origin)
            host = parsed.hostname or ""
            if host not in _LOCALHOST_HOSTS:
                return StarletteResponse(
                    content="Cross-origin request blocked",
                    status_code=403,
                )

            return await call_next(request)

    app.add_middleware(_CSRFMiddleware)

    @app.on_event("startup")
    async def _startup():
        """Ensure DB and registries are initialized (needed for uvicorn --reload)."""
        from mochi.db import init_db
        from mochi import skills as skill_registry
        from mochi import observers as observer_registry
        init_db()
        skill_registry.discover()
        observer_registry.discover()
        _migrate_encrypt_api_keys()
        # Seed model config from .env on first run (DB empty)
        from mochi.admin.admin_db import seed_models_from_env
        seed_models_from_env()
        # Auto-start the bot if a transport is already configured
        from mochi.admin.admin_env import read_env_value
        tg_token = (read_env_value("TELEGRAM_BOT_TOKEN") or "").strip()
        wx_enabled = (read_env_value("WEIXIN_ENABLED") or "").strip().lower() in ("1", "true", "yes")
        wx_token = (read_env_value("WEIXIN_BOT_TOKEN") or "").strip()
        if tg_token or (wx_enabled and wx_token):
            try:
                _start_bot_process()
            except Exception:
                pass  # non-fatal — user can restart from the portal

    def _migrate_encrypt_api_keys():
        """Encrypt any plaintext API keys in model_registry (idempotent)."""
        from mochi.admin.admin_crypto import is_encrypted, encrypt_api_key
        from mochi.db import _connect
        conn = _connect()
        rows = conn.execute(
            "SELECT name, api_key FROM model_registry WHERE api_key != ''"
        ).fetchall()
        migrated = 0
        for row in rows:
            if not is_encrypted(row["api_key"]):
                encrypted = encrypt_api_key(row["api_key"])
                if encrypted != row["api_key"]:  # encryption actually happened
                    conn.execute(
                        "UPDATE model_registry SET api_key = ? WHERE name = ?",
                        (encrypted, row["name"]),
                    )
                    migrated += 1
        if migrated:
            conn.commit()
            log.info("Encrypted %d plaintext API key(s) in model_registry", migrated)
        conn.close()

    # ── Auth ──────────────────────────────────────────────────────────────

    def _is_loopback(ip: str) -> bool:
        """Check if an IP is loopback (handles all IPv4/IPv6 variants)."""
        if ip in _LOCALHOST_HOSTS:
            return True
        try:
            import ipaddress
            return ipaddress.ip_address(ip).is_loopback
        except ValueError:
            return False

    _auth_failures: dict[str, list[float]] = {}  # {ip: [timestamps]}
    _AUTH_FAILURE_LIMIT = 10
    _AUTH_FAILURE_WINDOW = 300.0   # 5 minutes
    _AUTH_LOCKOUT_SECONDS = 60.0

    async def _verify_token(request: Request):
        """Token auth — required only for non-localhost access.

        Localhost connections (127.0.0.1, ::1) are trusted and skip auth.
        Remote connections require ADMIN_TOKEN.
        """
        from mochi.config import ADMIN_TOKEN

        # Localhost is trusted — no token needed
        client_ip = request.client.host if request.client else "unknown"
        if _is_loopback(client_ip):
            return

        if not ADMIN_TOKEN:
            raise HTTPException(
                status_code=403,
                detail="Remote access requires ADMIN_TOKEN. Set it in .env.",
            )
        now = time.monotonic()
        timestamps = _auth_failures.get(client_ip, [])
        timestamps[:] = [t for t in timestamps if now - t < _AUTH_FAILURE_WINDOW]

        if len(timestamps) >= _AUTH_FAILURE_LIMIT:
            oldest_lockout = timestamps[-_AUTH_FAILURE_LIMIT] + _AUTH_LOCKOUT_SECONDS
            if now < oldest_lockout:
                raise HTTPException(
                    status_code=429,
                    detail="Too many auth failures. Try again later.",
                )

        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token:
            token = request.query_params.get("token", "")
        if not hmac.compare_digest(token, ADMIN_TOKEN):
            timestamps.append(now)
            _auth_failures[client_ip] = timestamps
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

    # Separate rate limiter for QR poll (called every 3s, needs larger budget)
    _qr_poll_timestamps: list[float] = []
    _QR_POLL_RATE_LIMIT = 200   # max calls (~10 min of 3s polling)
    _QR_POLL_RATE_WINDOW = 600.0

    def _check_qr_poll_rate():
        now = time.monotonic()
        _qr_poll_timestamps[:] = [t for t in _qr_poll_timestamps if now - t < _QR_POLL_RATE_WINDOW]
        if len(_qr_poll_timestamps) >= _QR_POLL_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit: max {_QR_POLL_RATE_LIMIT} polls per {int(_QR_POLL_RATE_WINDOW)}s"
            )
        _qr_poll_timestamps.append(now)

    # ── Frontend ──────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def serve_frontend():
        html_path = Path(__file__).parent / "index.html"
        if not html_path.exists():
            raise HTTPException(status_code=404, detail="Frontend not found")
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-cache"},
        )

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
            OWNER_USER_ID,
            OURA_CLIENT_ID, OURA_CLIENT_SECRET, OURA_REFRESH_TOKEN,
            EMBEDDING_PROVIDER,
            AZURE_EMBEDDING_ENDPOINT, AZURE_EMBEDDING_API_KEY,
            DB_PATH,
            WEIXIN_ENABLED,
        )
        from mochi.admin.admin_env import env_key_is_set
        from mochi.admin.admin_env import read_env_value

        def _integration_status(name: str, keys: list[str]) -> dict:
            missing = [k for k in keys if not env_key_is_set(k)]
            return {"configured": len(missing) == 0, "missing": missing}

        # Check if models are configured: env vars OR DB tier assignments
        has_model_env = bool(CHAT_MODEL) and bool(CHAT_API_KEY)
        has_model_db = False
        tier_models = {}  # {tier: model_name} for configured tiers
        try:
            from mochi.admin.admin_db import get_tier_effective_config
            tier_config = get_tier_effective_config()
            for t_name in ("lite", "chat", "deep"):
                t_cfg = tier_config.get(t_name, {})
                if t_cfg.get("model") and t_cfg.get("api_key_set"):
                    tier_models[t_name] = t_cfg["model"]
            if tier_models:
                has_model_db = True
        except Exception:
            pass

        has_model = has_model_env or has_model_db
        chat_model_display = CHAT_MODEL or tier_models.get("chat", "")

        config_status = {
            "chat_model": {"set": has_model, "value": chat_model_display},
            "chat_api_key": {"set": has_model},
            "tier_models": tier_models,
            "chat_provider": {"set": bool(CHAT_PROVIDER), "value": CHAT_PROVIDER},
            "telegram_bot_token": {"set": bool(TELEGRAM_BOT_TOKEN) or bool((read_env_value("TELEGRAM_BOT_TOKEN") or "").strip())},
            "weixin_enabled": {"set": bool(WEIXIN_ENABLED) or (read_env_value("WEIXIN_ENABLED") or "").strip().lower() in ("1", "true", "yes")},
            "weixin_bot_token": {"set": bool((read_env_value("WEIXIN_BOT_TOKEN") or "").strip())},
            "owner_user_id": {
                "set": bool(OWNER_USER_ID) or bool((read_env_value("OWNER_USER_ID") or "").strip()),
                "value": str(OWNER_USER_ID) if OWNER_USER_ID else (read_env_value("OWNER_USER_ID") or ""),
            },
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

        # Basic config: considered "configured" if user has saved any override
        try:
            from mochi.admin.admin_db import get_system_overrides
            basic_configured = bool(get_system_overrides())
        except Exception:
            basic_configured = False

        return {
            "first_run": not has_model,
            "config_status": config_status,
            "basic_configured": basic_configured,
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
        pid = _start_bot_process()
        return {"ok": True, "pid": pid}

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
        return {"ok": True}

    # ── Embedding Config (shown on Models page) ─────────────────────────

    @app.get("/api/embedding/config", dependencies=[Depends(_verify_token)])
    async def api_get_embedding_config():
        """Return current embedding configuration read from .env file."""
        from mochi.admin.admin_env import read_env_value

        provider_raw = read_env_value("EMBEDDING_PROVIDER") or ""
        provider = provider_raw.strip().lower()

        # Legacy Azure auto-detect
        if not provider and read_env_value("AZURE_EMBEDDING_ENDPOINT"):
            provider = "azure_openai"

        api_key_val = read_env_value("EMBEDDING_API_KEY") or ""
        azure_api_key_val = read_env_value("AZURE_EMBEDDING_API_KEY") or ""

        # Determine configured status
        def _int_status(_name, keys):
            missing = [k for k in keys if not (read_env_value(k) or "").strip()]
            return {"configured": len(missing) == 0, "missing": missing}

        status = _embedding_integration_status(provider or provider_raw, _int_status)

        return {
            "provider": provider or "",
            "api_key_set": bool(api_key_val.strip()) or bool(azure_api_key_val.strip()),
            "model": read_env_value("EMBEDDING_MODEL") or "",
            "base_url": read_env_value("EMBEDDING_BASE_URL") or "",
            "azure_deployment": read_env_value("AZURE_EMBEDDING_DEPLOYMENT") or "text-embedding-3-small",
            "cache_max_size": int(read_env_value("EMBEDDING_CACHE_MAX_SIZE") or 128),
            "cache_ttl_s": int(read_env_value("EMBEDDING_CACHE_TTL_S") or 300),
            "configured": status.get("configured", False),
            "disabled": status.get("disabled", False),
        }

    @app.post("/api/embedding/test", dependencies=[Depends(_verify_token)])
    async def api_test_embedding():
        """Test embedding config by generating an embedding for a short string.

        Reads fresh values from .env (not cached config module) so the user
        can save and test without restarting.
        """
        _check_test_rate()
        from mochi.admin.admin_env import read_env_value
        from mochi.model_pool import _make_embed_client

        provider = (read_env_value("EMBEDDING_PROVIDER") or "").strip().lower()
        if not provider or provider == "none":
            return {"ok": False, "error": "Embedding 未配置（EMBEDDING_PROVIDER 为空或 none）"}

        # Resolve config from fresh .env values
        api_key = (read_env_value("EMBEDDING_API_KEY") or "").strip()
        model = (read_env_value("EMBEDDING_MODEL") or "").strip()
        base_url = (read_env_value("EMBEDDING_BASE_URL") or "").strip()

        if provider == "openai":
            model = model or "text-embedding-3-small"
        elif provider == "azure_openai":
            api_key = api_key or (read_env_value("AZURE_EMBEDDING_API_KEY") or "").strip()
            model = model or (read_env_value("AZURE_EMBEDDING_DEPLOYMENT") or "text-embedding-3-small").strip()
            base_url = base_url or (read_env_value("AZURE_EMBEDDING_ENDPOINT") or "").strip()
        elif provider == "ollama":
            api_key = api_key or "ollama"
            model = model or "nomic-embed-text"
            base_url = base_url or "http://localhost:11434/v1"
        else:
            return {"ok": False, "error": f"未知的 EMBEDDING_PROVIDER: {provider}"}

        try:
            client, eff_model = _make_embed_client(provider, api_key, model, base_url)
        except Exception as e:
            return {"ok": False, "error": f"创建客户端失败: {str(e)[:500]}"}

        if not client:
            return {"ok": False, "error": "Embedding 客户端未创建，请检查 API Key 和 Endpoint"}

        try:
            start = time.monotonic()
            resp = await asyncio.to_thread(
                client.embeddings.create, model=eff_model, input="hello",
            )
            elapsed = int((time.monotonic() - start) * 1000)
            dim = len(resp.data[0].embedding)
            return {"ok": True, "model": eff_model, "dim": dim, "latency_ms": elapsed}
        except Exception as e:
            return {"ok": False, "error": str(e)[:500]}

    # ═══════════════════════════════════════════════════════════════════════
    # Page 2: Heartbeat Config
    # ═══════════════════════════════════════════════════════════════════════

    _HEARTBEAT_PARAMS = {
        "HEARTBEAT_INTERVAL_MINUTES": "int",
        "MAX_DAILY_PROACTIVE": "int",
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
        if typ == "float":
            try:
                return float(value)
            except (ValueError, TypeError):
                return value
        return value

    # ═══════════════════════════════════════════════════════════════════════
    # Page 2b: Basic Config
    # ═══════════════════════════════════════════════════════════════════════

    _BASIC_PARAMS = {
        # Timezone
        "TIMEZONE_OFFSET_HOURS": "int",
        # Chat
        "AI_CHAT_MAX_COMPLETION_TOKENS": "int",
        # Maintenance
        "MAINTENANCE_HOUR": "int",
        "MAINTENANCE_ENABLED": "bool",
        "HEARTBEAT_LOG_TRIM_DAYS": "int",
        "HEARTBEAT_LOG_DELETE_DAYS": "int",
    }

    @app.get("/api/basic/config", dependencies=[Depends(_verify_token)])
    async def api_get_basic_config():
        import mochi.config as cfg
        from mochi.admin.admin_db import get_system_overrides
        overrides = get_system_overrides()
        result = {}
        for key, typ in _BASIC_PARAMS.items():
            env_val = getattr(cfg, key, None)
            override_val = overrides.get(key)
            result[key] = {
                "env_default": env_val,
                "override": override_val,
                "effective": _cast(override_val, typ) if override_val is not None else env_val,
                "type": typ,
            }
        return result

    @app.put("/api/basic/config", dependencies=[Depends(_verify_token)])
    async def api_set_basic_config(request: Request):
        body = await request.json()
        from mochi.admin.admin_db import set_system_override, clear_system_override
        updated = []
        for key, value in body.items():
            if key not in _BASIC_PARAMS:
                continue
            if value is None:
                clear_system_override(key)
            else:
                set_system_override(key, str(value))
            updated.append(key)
        return {"ok": True, "updated": updated}

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
        from mochi.skills import get_skill
        skill = get_skill(name)
        # Block disabling core skills
        if not enabled:
            if skill and getattr(skill, "core", False):
                return {"ok": False, "error": f"核心技能 {name} 不能关闭"}
        # Block enabling skills with missing required config
        if enabled:
            if skill and getattr(skill, "_config_missing", []):
                missing = ", ".join(skill._config_missing)
                return {"ok": False, "error": f"需要先配置: {missing}"}
        from mochi.db import set_skill_enabled
        set_skill_enabled(name, enabled)
        from mochi.skills import refresh_capability_summary
        refresh_capability_summary()
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
            # Recheck required env vars (config may now be satisfied)
            skill._config_missing = [
                key for key in skill.requires_config
                if not os.getenv(key) and not skill.config.get(key)
            ]
            # Re-enable co-located observer if config is now satisfied
            if not skill._config_missing:
                from mochi.observers import get_observer
                obs = get_observer(name)
                if obs and not obs.meta.enabled:
                    obs.meta.enabled = True
            # Capability summary may change (skill became available/unavailable)
            from mochi.skills import refresh_capability_summary
            refresh_capability_summary()

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
            if entry.get("internal"):
                continue
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

    @app.get("/api/skills/habit/habits", dependencies=[Depends(_verify_token)])
    async def api_list_habit_habits():
        """Return current habit list for display in admin panel (read-only)."""
        from mochi.config import OWNER_USER_ID
        from mochi.db import list_habits
        if not OWNER_USER_ID:
            return {"habits": []}
        rows = list_habits(OWNER_USER_ID, active_only=False)
        return {"habits": [
            {
                "id": h["id"],
                "name": h["name"],
                "frequency": h.get("frequency", "daily"),
                "importance": h.get("importance", "normal"),
                "context": h.get("context", ""),
                "active": bool(h.get("active", 1)),
                "paused_until": h.get("paused_until"),
            }
            for h in rows
        ]}

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

    # ── Telegram token test ──────────────────────────────────────────────

    @app.post("/api/telegram/test", dependencies=[Depends(_verify_token)])
    async def api_test_telegram(request: Request):
        """Test a Telegram Bot Token by calling getMe."""
        _check_test_rate()
        body = await request.json()
        token = (body.get("token") or "").strip()
        if not token:
            raise HTTPException(400, "token is required")
        if ":" not in token:
            raise HTTPException(400, "Invalid token format")
        try:
            from telegram import Bot
        except ImportError:
            raise HTTPException(501, "python-telegram-bot not installed")
        try:
            bot = Bot(token=token)
            me = await asyncio.wait_for(bot.get_me(), timeout=10)
            return {"ok": True, "username": me.username or "", "first_name": me.first_name or ""}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Connection timed out (10s)"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:500]}

    # ── WeChat token test ───────────────────────────────────────────────

    @app.post("/api/weixin/test", dependencies=[Depends(_verify_token)])
    async def api_test_weixin(request: Request):
        """Test a WeChat Bot Token by calling the getconfig API."""
        _check_test_rate()
        body = await request.json()
        token = (body.get("token") or "").strip()
        if not token:
            raise HTTPException(400, "token is required")
        try:
            import aiohttp
        except ImportError:
            raise HTTPException(501, "aiohttp not installed (pip install aiohttp)")
        import struct, os, base64
        uint32 = struct.unpack(">I", os.urandom(4))[0]
        uin = base64.b64encode(str(uint32).encode()).decode()
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {token}",
            "X-WECHAT-UIN": uin,
        }
        base_url = (body.get("base_url") or "https://ilinkai.weixin.qq.com").rstrip("/")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base_url}/ilink/bot/getconfig",
                    json={"ilink_user_id": "", "context_token": ""},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
                    ret = data.get("ret", -1)
                    errcode = data.get("errcode", 0)
                    if ret == 0 and errcode == 0:
                        return {"ok": True}
                    if errcode == -14 or ret == -14:
                        return {"ok": False, "error": "Token expired — re-run weixin_auth.py"}
                    return {"ok": False, "error": f"API error: ret={ret} errcode={errcode}"}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Connection timed out (10s)"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:500]}

    # ── WeChat QR auth flow ────────────────────────────────────────────

    _WEIXIN_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"

    @app.post("/api/weixin/qr", dependencies=[Depends(_verify_token)])
    async def api_weixin_qr():
        """Fetch a QR code for WeChat bot login."""
        _check_test_rate()
        try:
            import aiohttp
        except ImportError:
            raise HTTPException(501, "aiohttp not installed (pip install aiohttp)")
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{_WEIXIN_DEFAULT_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type=3"
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json(content_type=None)
                    qrcode = data.get("qrcode", "")
                    qr_content = data.get("qrcode_img_content", "")
                    if not qrcode or not qr_content:
                        return {"ok": False, "error": f"API returned no QR data: {str(data)[:200]}"}
                    return {"ok": True, "qrcode": qrcode, "qrcode_img_content": qr_content}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Connection timed out (15s)"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:500]}

    @app.post("/api/weixin/qr/poll", dependencies=[Depends(_verify_token)])
    async def api_weixin_qr_poll(request: Request):
        """Poll QR code scan status. On confirmed, auto-save credentials to .env."""
        _check_qr_poll_rate()
        body = await request.json()
        qrcode = (body.get("qrcode") or "").strip()
        if not qrcode:
            raise HTTPException(400, "qrcode is required")
        try:
            import aiohttp
        except ImportError:
            raise HTTPException(501, "aiohttp not installed (pip install aiohttp)")
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{_WEIXIN_DEFAULT_BASE_URL}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
                async with session.get(
                    url,
                    headers={"iLink-App-ClientVersion": "1"},
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as resp:
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            return {"status": "wait"}
        except Exception as e:
            return {"status": "error", "error": str(e)[:500]}

        status = data.get("status", "")

        if status == "confirmed":
            from mochi.admin.admin_env import write_env_value, read_env_value
            bot_token = data.get("bot_token", "")
            user_id = data.get("ilink_user_id", "")
            base_url = data.get("baseurl", "")

            if bot_token:
                write_env_value("WEIXIN_ENABLED", "true")
                write_env_value("WEIXIN_BOT_TOKEN", bot_token)

            if base_url and base_url.rstrip("/") != _WEIXIN_DEFAULT_BASE_URL:
                write_env_value("WEIXIN_BASE_URL", base_url)

            if user_id:
                existing = read_env_value("WEIXIN_ALLOWED_USERS")
                if not existing:
                    write_env_value("WEIXIN_ALLOWED_USERS", user_id)

            log.info("WeChat QR auth: credentials saved to .env")

        return {
            "status": status,
            "bot_token": data.get("bot_token", ""),
            "ilink_user_id": data.get("ilink_user_id", ""),
            "baseurl": data.get("baseurl", ""),
        }

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

    # ═══════════════════════════════════════════════════════════════════════
    # Page 5: Memory
    # ═══════════════════════════════════════════════════════════════════════

    @app.get("/api/memory", dependencies=[Depends(_verify_token)])
    async def api_get_memory():
        """Return core memory content."""
        from mochi.config import OWNER_USER_ID
        from mochi.db import get_core_memory
        uid = OWNER_USER_ID or 0
        content = get_core_memory(uid)
        return {"content": content, "chars": len(content)}

    @app.post("/api/memory", dependencies=[Depends(_verify_token)])
    async def api_save_memory(request: Request):
        """Update core memory content."""
        from mochi.config import OWNER_USER_ID
        from mochi.db import get_core_memory, update_core_memory
        body = await request.json()
        content = body.get("content", "")
        uid = OWNER_USER_ID or 0
        update_core_memory(uid, content)
        log.info("Admin: updated core memory (%d chars)", len(content))
        return {"ok": True, "chars": len(content)}

    @app.get("/api/memory-items", dependencies=[Depends(_verify_token)])
    async def api_get_memory_items(
        q: str = "", category: str = "", sort: str = "importance",
        page: int = 1, limit: int = 20,
    ):
        """Browse L2 memory_items with keyword search, category filter, pagination."""
        from mochi.config import OWNER_USER_ID
        from mochi.db import _connect

        uid = OWNER_USER_ID or 0
        page = max(1, page)
        limit = max(1, min(limit, 100))

        conn = _connect()
        conditions = ["user_id = ?"]
        params: list = [uid]
        if q:
            conditions.append("content LIKE ?")
            params.append(f"%{q}%")
        if category:
            conditions.append("category = ?")
            params.append(category)
        where = " AND ".join(conditions)

        order = "importance DESC, updated_at DESC" if sort == "importance" else "updated_at DESC"

        total = conn.execute(
            f"SELECT COUNT(*) as cnt FROM memory_items WHERE {where}", params
        ).fetchone()["cnt"]

        offset = (page - 1) * limit
        rows = conn.execute(
            f"SELECT id, category, content, importance, access_count, source, "
            f"created_at, updated_at FROM memory_items "
            f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        cat_rows = conn.execute(
            "SELECT category, COUNT(*) as cnt FROM memory_items "
            "WHERE user_id = ? GROUP BY category ORDER BY cnt DESC",
            (uid,),
        ).fetchall()
        conn.close()

        return {
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, (total + limit - 1) // limit),
            "categories": [{"name": r["category"] or "(无)", "count": r["cnt"]} for r in cat_rows],
            "items": [
                {
                    "id": r["id"],
                    "category": r["category"],
                    "content": r["content"],
                    "importance": r["importance"],
                    "access_count": r["access_count"],
                    "source": r["source"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ],
        }

    @app.post("/api/memory-items/delete", dependencies=[Depends(_verify_token)])
    async def api_delete_memory_items(request: Request):
        """Delete one or more L2 memory items (soft-delete to trash)."""
        from mochi.db import delete_memory_items
        body = await request.json()
        ids = body.get("ids", [])
        if not ids:
            raise HTTPException(400, "No item ids provided")
        count = delete_memory_items(ids, deleted_by="admin")
        log.info("Admin: deleted %d memory items ids=%s", count, ids)
        return {"ok": True, "count": count}

    @app.post("/api/memory-items/{item_id}", dependencies=[Depends(_verify_token)])
    async def api_update_memory_item(item_id: int, request: Request):
        """Edit a single L2 memory item."""
        from mochi.config import OWNER_USER_ID
        from mochi.db import _connect
        from datetime import datetime
        from mochi.config import TZ

        body = await request.json()
        uid = OWNER_USER_ID or 0
        now = datetime.now(TZ).isoformat()
        conn = _connect()

        row = conn.execute(
            "SELECT id FROM memory_items WHERE id = ? AND user_id = ?",
            (item_id, uid),
        ).fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Memory item {item_id} not found")

        content = body.get("content", "").strip()
        category = body.get("category", "").strip()
        importance = max(1, min(3, int(body.get("importance", 1))))

        conn.execute(
            "UPDATE memory_items SET content = ?, category = ?, importance = ?, "
            "updated_at = ? WHERE id = ?",
            (content, category, importance, now, item_id),
        )
        conn.commit()
        conn.close()
        log.info("Admin: updated memory item #%d cat=%s imp=%d", item_id, category, importance)
        return {"ok": True, "id": item_id}


# ═══════════════════════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════════════════════

async def start_admin_server(port: int = 8080, bind: str = "127.0.0.1"):
    """Start the admin portal as an async task."""
    if not HAS_FASTAPI:
        raise ImportError("fastapi/uvicorn not installed")

    _LOCALHOST = {"127.0.0.1", "localhost", "::1"}
    if bind not in _LOCALHOST:
        log.warning(
            "Admin portal binding to %s — exposed to network. "
            "Consider a reverse proxy with HTTPS.", bind
        )

    import uvicorn
    config = uvicorn.Config(
        app, host=bind, port=port,
        log_level="warning",  # don't spam bot logs with HTTP access logs
    )
    server = uvicorn.Server(config)
    await server.serve()
