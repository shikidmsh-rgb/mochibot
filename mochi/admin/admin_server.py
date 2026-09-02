"""MochiBot Admin Portal — FastAPI server.

Provides a web-based setup & configuration portal for MochiBot.
All endpoints are under /api, with optional token auth via ADMIN_TOKEN.
"""

import asyncio
import hmac
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from mochi.admin_access import sanitize_persistent_text

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

# The bot and admin share one process. main.py supplies the small runtime
# hooks needed by the status and stop endpoints.
_runtime_status_provider = lambda: {"running": False, "pid": os.getpid()}


def register_runtime_controls(status_provider) -> None:
    global _runtime_status_provider
    _runtime_status_provider = status_provider


def _get_app_version() -> str:
    from mochi._version import read_version
    return read_version()


def _update_sync_failure(
    python_executable: str,
    pre_hash: str,
    *,
    project_root: Path = _PROJECT_ROOT,
    timeout: int = 300,
) -> dict | None:
    """Install requirements with the same environment contract as setup."""
    sync_command = [
        python_executable,
        "-m",
        "pip",
        "install",
        "-r",
        "requirements.txt",
    ]
    try:
        result = subprocess.run(
            sync_command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(project_root),
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        detail = f"依赖安装超过 {timeout} 秒，已停止等待。"
    except OSError as exc:
        detail = f"无法启动依赖安装：{exc}"
    else:
        if result.returncode == 0:
            return None
        detail = (
            f"依赖安装失败："
            f"{((result.stdout or '') + (result.stderr or '')).strip()[:500]}"
        )
    return {
        "ok": False,
        "error": detail,
        "pre_hash": pre_hash,
        "hint": (
            "代码已更新但依赖未完成。请先重新运行 setup.bat 或 bash setup.sh；"
            f"如需回退，请确认本地改动已备份后再执行 git reset --hard {pre_hash}"
        ),
        "code_updated": True,
    }


def _format_embedding_test_error(exc: Exception) -> dict:
    """Turn provider exceptions into concise, actionable UI errors."""
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            code = code or detail.get("code")

    code_text = str(code or "").strip()
    raw = f"{code_text} {exc}".lower()
    if code_text.lower() == "unknown_model" or "unknown_model" in raw:
        message = (
            "模型名不匹配或服务暂时未就绪。请先重试一次；"
            "持续失败时检查服务端的模型部署名。"
        )
    elif status == 401 or "invalid_api_key" in raw or "incorrect api key" in raw:
        message = "API Key 不正确，请检查后重试。"
    elif (
        status == 404
        or "resource_not_found" in raw
        or "not found" in raw
    ):
        message = "Base URL / API 路径或模型部署不存在，请检查配置。"
    else:
        message = "连接失败，请检查 Base URL、模型名和服务状态。"

    result = {"ok": False, "error": message}
    if code_text:
        result["code"] = code_text[:80]
    if isinstance(status, int):
        result["status"] = status
    return result


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
        from mochi.config import OWNER_USER_ID
        from mochi.core_store import initialize_core
        initialize_core(OWNER_USER_ID or 0)
        skill_registry.discover()
        observer_registry.discover()
        _migrate_encrypt_api_keys()
        # Seed model config from .env on first run (DB empty)
        from mochi.admin.admin_db import seed_models_from_env
        seed_models_from_env()
        # Seed system config from .env on first run (DB empty)
        from mochi.admin.admin_db import seed_system_config_from_env
        seed_system_config_from_env()

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

    def _embedding_integration_status(
        provider: str,
        base_url: str,
        _int_status,
    ) -> dict:
        """Dynamic integration status based on EMBEDDING_PROVIDER."""
        p = (provider or "").strip().lower()
        if not p or p == "none":
            return {"configured": False, "missing": [], "disabled": True}
        if p == "openai":
            from mochi.model_pool import _is_supported_embedding_base_url
            if not _is_supported_embedding_base_url(base_url):
                return {
                    "configured": False,
                    "missing": [],
                    "unsupported": True,
                }
            return _int_status(
                "embedding", ["EMBEDDING_API_KEY", "EMBEDDING_MODEL"]
            )
        return {
            "configured": False,
            "missing": ["EMBEDDING_PROVIDER"],
            "unsupported": True,
        }

    @app.get("/api/status", dependencies=[Depends(_verify_token)])
    async def get_status():
        from mochi.config import OWNER_USER_ID, DB_PATH
        from mochi.admin.admin_env import read_env_value

        has_required_models = False
        main_ready = False
        tier_models = {}  # {tier: model_name} for configured tiers
        unsupported_tiers = {}
        main_tier = {}
        try:
            from mochi.admin.admin_db import (
                are_required_tiers_ready,
                get_tier_effective_config,
                is_main_tier_ready,
            )
            tier_config = get_tier_effective_config()
            main_tier = tier_config.get("main", {})
            for t_name in ("main", "lite"):
                t_cfg = tier_config.get(t_name, {})
                if t_cfg.get("model"):
                    tier_models[t_name] = t_cfg["model"]
                if t_cfg.get("assigned_name") and not t_cfg.get("supported"):
                    unsupported_tiers[t_name] = t_cfg.get("provider", "")
            main_ready = is_main_tier_ready(tier_config)
            has_required_models = are_required_tiers_ready(tier_config)
        except Exception:
            pass

        config_status = {
            "main_model": {"set": main_ready, "value": main_tier.get("model", "")},
            "main_api_key": {"set": main_ready},
            "tier_models": tier_models,
            "unsupported_tiers": unsupported_tiers,
            "main_provider": {
                "set": bool(main_tier.get("provider")),
                "value": main_tier.get("provider", ""),
            },
            "telegram_bot_token": {"set": bool((read_env_value("TELEGRAM_BOT_TOKEN") or "").strip())},
            "weixin_enabled": {"set": (read_env_value("WEIXIN_ENABLED") or "").strip().lower() in ("1", "true", "yes")},
            "weixin_bot_token": {"set": bool((read_env_value("WEIXIN_BOT_TOKEN") or "").strip())},
            "owner_user_id": {
                "set": bool(OWNER_USER_ID) or bool((read_env_value("OWNER_USER_ID") or "").strip()),
                "value": str(OWNER_USER_ID) if OWNER_USER_ID else (read_env_value("OWNER_USER_ID") or ""),
            },
        }

        # Heartbeat state
        try:
            from mochi.heartbeat import get_stats
            hb = get_stats()
        except Exception:
            hb = {"state": "UNKNOWN"}

        # ── Database & error stats ──
        import os as _os
        db_exists = DB_PATH.exists()
        db_size = DB_PATH.stat().st_size if db_exists else 0
        db_writable = _os.access(str(DB_PATH), _os.W_OK) if db_exists else False
        try:
            from mochi.error_buffer import get_recent_errors
            recent_error_count = len(get_recent_errors(24))
        except Exception:
            recent_error_count = 0

        has_transport = bool((read_env_value("TELEGRAM_BOT_TOKEN") or "").strip()) or (
            (read_env_value("WEIXIN_ENABLED") or "").strip().lower()
            in ("1", "true", "yes")
            and bool((read_env_value("WEIXIN_BOT_TOKEN") or "").strip())
        )

        return {
            "first_run": not has_required_models,
            "setup_mode": not has_required_models and has_transport,
            "config_status": config_status,
            "heartbeat_state": hb.get("state", "UNKNOWN"),
            "db_path": str(DB_PATH),
            "db_size_bytes": db_size,
            "db_writable": db_writable,
            "recent_error_count": recent_error_count,
            "version": _get_app_version(),
            "git_available": (_PROJECT_ROOT / ".git").exists(),
        }

    # ═══════════════════════════════════════════════════════════════════════
    # Runtime control
    # ═══════════════════════════════════════════════════════════════════════

    @app.post("/api/bot/start", dependencies=[Depends(_verify_token)])
    async def api_bot_start():
        from mochi.shutdown import request_restart, set_agent_enabled
        set_agent_enabled(True)
        request_restart()
        return {"ok": True, "enabled": True, "restarting": True}

    @app.post("/api/bot/stop", dependencies=[Depends(_verify_token)])
    async def api_bot_stop():
        from mochi.shutdown import request_restart, set_agent_enabled
        set_agent_enabled(False)
        request_restart()
        return {"ok": True, "enabled": False, "restarting": True}

    @app.get("/api/bot/status", dependencies=[Depends(_verify_token)])
    async def api_bot_status():
        try:
            status = dict(_runtime_status_provider())
        except Exception as exc:
            log.warning("Runtime status provider failed: %s", exc)
            status = {"running": False, "pid": os.getpid()}
        status.setdefault("running", False)
        status.setdefault("enabled", status["running"])
        status.setdefault("pid", os.getpid())
        status.setdefault("weixin_session_expired", False)
        return status

    @app.post("/api/admin/restart", dependencies=[Depends(_verify_token)])
    async def api_admin_restart():
        """Restart the single Mochi process."""
        from mochi.shutdown import request_restart
        request_restart()
        return {"ok": True, "message": "Mochi restarting..."}

    # ── System update ────────────────────────────────────────────────────

    _update_timestamps: list[float] = []
    _UPDATE_RATE_LIMIT = 3
    _UPDATE_RATE_WINDOW = 60.0

    def _check_update_rate():
        now = time.monotonic()
        _update_timestamps[:] = [t for t in _update_timestamps if now - t < _UPDATE_RATE_WINDOW]
        if len(_update_timestamps) >= _UPDATE_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="请等待 60 秒后再试")
        _update_timestamps.append(now)

    def _run_git(*args: str, timeout: int = 30) -> tuple[int, str]:
        """Run a git command with fixed args (no shell). Returns (returncode, output)."""
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True, timeout=timeout,
            cwd=str(_PROJECT_ROOT), encoding="utf-8", errors="replace",
        )
        return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()

    def _is_git_worktree() -> bool:
        rc, output = _run_git("rev-parse", "--is-inside-work-tree")
        return rc == 0 and output.strip().lower() == "true"

    @app.post("/api/system/update-check", dependencies=[Depends(_verify_token)])
    async def api_system_update_check():
        _check_update_rate()
        if not await asyncio.to_thread(_is_git_worktree):
            return {"ok": False, "error": "当前安装不是 Git 仓库，无法通过此方式更新。请使用命令行手动更新。"}

        # Fetch latest from remote
        rc, out = await asyncio.to_thread(
            _run_git, "fetch", "origin", "main", timeout=30
        )
        if rc != 0:
            return {"ok": False, "error": f"无法连接远程仓库：{out}"}

        # Count commits behind
        rc, count_str = await asyncio.to_thread(
            _run_git, "rev-list", "--count", "HEAD..origin/main"
        )
        if rc != 0:
            return {"ok": False, "error": f"无法对比版本：{count_str}"}
        commits_behind = int(count_str) if count_str.isdigit() else 0

        # Current version + commit
        current_version = _get_app_version()
        rc, current_hash = await asyncio.to_thread(
            _run_git, "rev-parse", "--short", "HEAD"
        )
        current_hash = current_hash if rc == 0 else ""

        # Remote version: read __init__.py from origin/main
        remote_version = current_version
        if commits_behind > 0:
            rc, remote_init = await asyncio.to_thread(
                _run_git, "show", "origin/main:mochi/__init__.py"
            )
            if rc == 0:
                import re
                m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', remote_init)
                if m:
                    remote_version = m.group(1)

        # Changelog diff
        changelog_diff = ""
        if commits_behind > 0:
            rc, diff_out = await asyncio.to_thread(
                _run_git, "diff", "HEAD..origin/main", "--", "CHANGELOG.md"
            )
            if rc == 0 and diff_out:
                # Extract only added lines (new changelog entries)
                added = [ln[1:] for ln in diff_out.split("\n") if ln.startswith("+") and not ln.startswith("+++")]
                changelog_diff = "\n".join(added).strip()

        return {
            "ok": True,
            "available": commits_behind > 0,
            "current_version": current_version,
            "current_hash": current_hash,
            "remote_version": remote_version,
            "commits_behind": commits_behind,
            "changelog_diff": changelog_diff,
        }

    @app.post("/api/system/update-apply", dependencies=[Depends(_verify_token)])
    async def api_system_update_apply():
        _check_update_rate()
        if not await asyncio.to_thread(_is_git_worktree):
            return {"ok": False, "error": "当前安装不是 Git 仓库。"}

        # Check for dirty working tree (ignore untracked files)
        rc, status_out = await asyncio.to_thread(
            _run_git, "status", "--porcelain"
        )
        if rc != 0:
            return {"ok": False, "error": f"无法检查工作区状态：{status_out}"}
        dirty_lines = [l for l in status_out.splitlines() if not l.startswith("??")]
        if dirty_lines:
            return {
                "ok": False,
                "error": "检测到本地代码改动，请先处理后再更新。",
                "dirty_files": "\n".join(dirty_lines),
                "hint": "在终端执行 git stash（暂存改动）或 git checkout .（放弃改动），然后再试。",
            }

        # Record pre-update hash for rollback reference
        rc, pre_hash = await asyncio.to_thread(
            _run_git, "rev-parse", "--short", "HEAD"
        )
        pre_hash = pre_hash if rc == 0 else "unknown"

        import shutil

        # Pull
        rc, pull_out = await asyncio.to_thread(
            _run_git, "pull", "origin", "main", timeout=60
        )
        if rc != 0:
            return {"ok": False, "error": f"拉取代码失败：{pull_out}", "pre_hash": pre_hash}

        # Clean stale __pycache__ so restart loads new bytecode, not cached old .pyc
        def _clean_pycache() -> None:
            for pycache in (_PROJECT_ROOT / "mochi").rglob("__pycache__"):
                shutil.rmtree(pycache, ignore_errors=True)

        await asyncio.to_thread(_clean_pycache)

        # Install dependencies
        sync_failure = await asyncio.to_thread(
            _update_sync_failure, sys.executable, pre_hash
        )
        if sync_failure:
            return sync_failure

        # Get new version
        rc, new_init = await asyncio.to_thread(
            _run_git, "show", "HEAD:mochi/__init__.py"
        )
        new_version = _get_app_version()
        if rc == 0:
            import re
            m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', new_init)
            if m:
                new_version = m.group(1)

        # Schedule restart so the new version actually takes effect.
        # Without this, the running process keeps the old mochi/__init__.py
        # cached in sys.modules and the UI shows stale version forever.
        from mochi.shutdown import request_restart
        loop = asyncio.get_event_loop()
        loop.call_later(3, request_restart)

        return {
            "ok": True,
            "message": "更新完成！3 秒后自动重启……",
            "restarting": True,
            "pre_hash": pre_hash,
            "new_version": new_version,
            "pull_output": pull_out[:500],
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
        provider = body.get("provider", "openai")
        base_url = body.get("base_url", "")
        api_key = body.get("api_key", "")
        credential_source = body.get("credential_source_model", "").strip()
        from mochi.admin.admin_db import (
            get_model,
            list_tier_assignments,
            upsert_model,
        )
        if credential_source:
            if credential_source == name:
                raise HTTPException(400, "credential source must be another model")
            source = get_model(credential_source, mask_key=False)
            if not source:
                raise HTTPException(400, "credential source model not found")
            source_url = (source.get("base_url") or "").strip().rstrip("/")
            requested_url = (base_url or "").strip().rstrip("/")
            if source["provider"] != provider or source_url != requested_url:
                raise HTTPException(
                    400,
                    "credential source must use the same provider and endpoint",
                )
            if not source.get("api_key"):
                raise HTTPException(400, "credential source has no API key")
            api_key = source["api_key"]
            base_url = source["base_url"]
        try:
            upsert_model(
                name=name,
                provider=provider,
                model=body.get("model", ""),
                api_key=api_key,
                base_url=base_url,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))

        assigned_tiers = [
            tier
            for tier, assigned_name in list_tier_assignments().items()
            if assigned_name == name
        ]
        if assigned_tiers:
            entry = get_model(name, mask_key=False)
            if entry:
                from mochi.model_pool import get_pool
                for tier in assigned_tiers:
                    try:
                        get_pool().reload_tier(
                            tier,
                            entry["provider"],
                            entry["api_key"],
                            entry["model"],
                            entry["base_url"],
                        )
                    except Exception as exc:
                        log.warning("Tier hot-reload failed for '%s': %s", tier, exc)

        restart_required = (
            bool(_runtime_status_provider().get("setup_mode", False))
            and "main" in assigned_tiers
        )
        return {"ok": True, "restart_required": restart_required}

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
            log.warning("Model test failed for '%s': %s", name, err_str[:300])
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
        restart_required = (
            tier == "main"
            and bool(_runtime_status_provider().get("setup_mode", False))
        )
        return {"ok": True, "restart_required": restart_required}

    @app.delete("/api/tiers/{tier}", dependencies=[Depends(_verify_token)])
    async def api_clear_tier(tier: str):
        from mochi.admin.admin_db import clear_tier_assignment
        try:
            clear_tier_assignment(tier)
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            from mochi.model_pool import get_pool
            get_pool().clear_tier(tier)
        except Exception as e:
            log.warning("Tier hot-clear failed for '%s': %s", tier, e)
        return {"ok": True}

    # ── Embedding Config (shown on Models page) ─────────────────────────

    @app.get("/api/embedding/config", dependencies=[Depends(_verify_token)])
    async def api_get_embedding_config():
        """Return current embedding configuration read from .env file."""
        from mochi.admin.admin_env import read_env_value

        provider_raw = read_env_value("EMBEDDING_PROVIDER") or "none"
        provider = provider_raw.strip().lower()

        api_key_val = read_env_value("EMBEDDING_API_KEY") or ""
        base_url = read_env_value("EMBEDDING_BASE_URL") or ""

        # Determine configured status
        def _int_status(_name, keys):
            missing = [k for k in keys if not (read_env_value(k) or "").strip()]
            return {"configured": len(missing) == 0, "missing": missing}

        status = _embedding_integration_status(
            provider or provider_raw,
            base_url,
            _int_status,
        )

        return {
            "provider": provider or "none",
            "api_key_set": bool(api_key_val.strip()),
            "model": read_env_value("EMBEDDING_MODEL") or "",
            "base_url": base_url,
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

        if provider != "openai":
            return {"ok": False, "error": f"未知的 EMBEDDING_PROVIDER: {provider}"}
        if not api_key or not model:
            return {"ok": False, "error": "Embedding 需要 API Key 和模型名"}

        try:
            client, eff_model = _make_embed_client(provider, api_key, model, base_url)
        except Exception as e:
            return _format_embedding_test_error(e)

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
            return _format_embedding_test_error(e)

    # ═══════════════════════════════════════════════════════════════════════
    # User preferences
    # ═══════════════════════════════════════════════════════════════════════

    def _preference_payload() -> dict:
        from mochi.admin.admin_db import (
            SYSTEM_DEFAULTS,
            get_system_config,
            get_system_overrides,
        )
        from mochi.admin.preferences import (
            PREFERENCE_KEYS,
            resolve_free_time_capacity,
        )

        overrides = get_system_overrides()
        values = {key: get_system_config(key) for key in PREFERENCE_KEYS}
        try:
            free_time_max = resolve_free_time_capacity(values)
        except ValueError:
            from mochi.config import FREE_TIME_DAILY_MAX
            free_time_max = FREE_TIME_DAILY_MAX
        payload = {}
        for key in PREFERENCE_KEYS:
            item = {
                "value": values[key],
                "default": SYSTEM_DEFAULTS[key][1],
                "source": "user" if key in overrides else "default",
            }
            if key == "TIMEZONE_OFFSET_HOURS":
                item["kind"] = "float"
                item["min"] = -12.0
                item["max"] = 14.0
            elif key == "MAX_DAILY_FREE_TIME":
                item["kind"] = "int"
                item["min"] = 0
                item["max"] = free_time_max
            elif key in {"SLEEP_AFTER_HOUR", "WAKE_EARLIEST_HOUR"}:
                item["kind"] = "hour"
                item["min"] = 0
                item["max"] = 23
            else:
                item["kind"] = "clock"
            payload[key] = item
        return payload

    @app.get("/api/preferences", dependencies=[Depends(_verify_token)])
    async def api_get_preferences():
        return _preference_payload()

    @app.put("/api/preferences", dependencies=[Depends(_verify_token)])
    async def api_set_preferences(request: Request):
        from mochi.admin.admin_db import get_system_config, set_system_override
        from mochi.admin.preferences import (
            PREFERENCE_KEYS,
            normalize_preference_updates,
        )

        body = await request.json()
        current = {key: get_system_config(key) for key in PREFERENCE_KEYS}
        try:
            normalized = normalize_preference_updates(body, current)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        for key, value in normalized.items():
            set_system_override(key, value)
        return {"ok": True, "updated": list(normalized)}

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
            async with httpx.AsyncClient() as client:
                async with asyncio.timeout(10):
                    resp = await client.post(
                        f"{base_url}/ilink/bot/getconfig",
                        json={"ilink_user_id": "", "context_token": ""},
                        headers=headers,
                        timeout=10,
                    )
                resp.raise_for_status()
                data = resp.json()
                ret = data.get("ret", -1)
                errcode = data.get("errcode", 0)
                if ret == 0 and errcode == 0:
                    return {"ok": True}
                if errcode == -14 or ret == -14:
                    return {"ok": False, "error": "Token expired — re-run scripts/weixin_auth.py"}
                return {"ok": False, "error": f"API error: ret={ret} errcode={errcode}"}
        except (asyncio.TimeoutError, httpx.TimeoutException):
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
            async with httpx.AsyncClient() as client:
                url = f"{_WEIXIN_DEFAULT_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type=3"
                async with asyncio.timeout(15):
                    resp = await client.get(url, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                qrcode = data.get("qrcode", "")
                qr_content = data.get("qrcode_img_content", "")
                if not qrcode or not qr_content:
                    return {"ok": False, "error": f"API returned no QR data: {str(data)[:200]}"}
                return {"ok": True, "qrcode": qrcode, "qrcode_img_content": qr_content}
        except (asyncio.TimeoutError, httpx.TimeoutException):
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
            async with httpx.AsyncClient() as client:
                url = f"{_WEIXIN_DEFAULT_BASE_URL}/ilink/bot/get_qrcode_status"
                async with asyncio.timeout(35):
                    resp = await client.get(
                        url,
                        params={"qrcode": qrcode},
                        headers={"iLink-App-ClientVersion": "1"},
                        timeout=35,
                    )
                resp.raise_for_status()
                data = resp.json()
        except (asyncio.TimeoutError, httpx.TimeoutException):
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
    # Memory
    # ═══════════════════════════════════════════════════════════════════════

    @app.get("/api/memory", dependencies=[Depends(_verify_token)])
    async def api_get_memory():
        """Return the complete Core and migration state."""
        from mochi.core_store import (
            get_core_hygiene_status,
            get_core_migration_status,
            get_core_stats,
            read_core,
        )
        return {
            "content": read_core(),
            **get_core_stats(),
            "migration": get_core_migration_status(),
            "hygiene": get_core_hygiene_status(),
        }

    @app.post("/api/memory", dependencies=[Depends(_verify_token)])
    async def api_save_core(request: Request):
        """Replace Core through its budgeted, snapshotting API."""
        from mochi.core_store import CoreError, replace_core
        body = await request.json()
        content = body.get("content", "")
        try:
            result = replace_core(content, source="admin")
        except CoreError as exc:
            raise HTTPException(400, str(exc))
        log.info("Admin: updated Core (%d chars)", result["chars"])
        return {"ok": True, **result}

    @app.get("/api/memory-items", dependencies=[Depends(_verify_token)])
    async def api_get_memory_items(
        q: str = "", sort: str = "importance",
        page: int = 1, limit: int = 20,
    ):
        """Browse Memory Items with keyword search and pagination."""
        from mochi.config import OWNER_USER_ID
        from mochi.db import _connect, get_memory_evidence_dates

        uid = OWNER_USER_ID or 0
        page = max(1, page)
        limit = max(1, min(limit, 100))

        conn = _connect()
        conditions = ["user_id = ?"]
        params: list = [uid]
        if q:
            conditions.append("content LIKE ?")
            params.append(f"%{q}%")
        where = " AND ".join(conditions)

        order = "importance DESC, updated_at DESC" if sort == "importance" else "updated_at DESC"

        total = conn.execute(
            f"SELECT COUNT(*) as cnt FROM memory_items WHERE {where}", params
        ).fetchone()["cnt"]

        offset = (page - 1) * limit
        rows = conn.execute(
            f"SELECT id, content, importance, access_count, source, "
            f"created_at, updated_at FROM memory_items "
            f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        conn.close()
        evidence_dates = get_memory_evidence_dates(
            uid, [int(row["id"]) for row in rows],
        )

        return {
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, (total + limit - 1) // limit),
            "items": [
                {
                    "id": r["id"],
                    "content": r["content"],
                    "importance": r["importance"],
                    "access_count": r["access_count"],
                    "source": r["source"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    **evidence_dates.get(r["id"], {
                        "evidence_start": "",
                        "evidence_end": "",
                    }),
                }
                for r in rows
            ],
        }

    @app.get(
        "/api/memory-extraction-status",
        dependencies=[Depends(_verify_token)],
    )
    async def api_memory_extraction_status():
        """Expose the owner-facing progress and latest extraction failure."""
        from mochi.config import OWNER_USER_ID
        from mochi.memory_extraction import EXTRACTION_BATCH_SIZE
        from mochi.db import get_memory_extraction_status

        return get_memory_extraction_status(
            OWNER_USER_ID or 0, EXTRACTION_BATCH_SIZE,
        )

    @app.get(
        "/api/conversation-summary-status",
        dependencies=[Depends(_verify_token)],
    )
    async def api_conversation_summary_status():
        """Expose rolling summary progress and the latest background failure."""
        from mochi.config import OWNER_USER_ID
        from mochi.conversation_summary import SUMMARY_BATCH_SIZE
        from mochi.db import get_conversation_summary_status

        return get_conversation_summary_status(
            OWNER_USER_ID or 0, SUMMARY_BATCH_SIZE,
        )

    @app.get(
        "/api/memory-items/{item_id}/evidence",
        dependencies=[Depends(_verify_token)],
    )
    async def api_get_memory_item_evidence(item_id: int):
        """Return one owner's recorded source messages for lazy receipt display."""
        from mochi.config import OWNER_USER_ID
        from mochi.db import get_memory_evidence_receipt

        receipt = get_memory_evidence_receipt(
            OWNER_USER_ID or 0,
            item_id,
            max_message_chars=2000,
        )
        if receipt is None:
            raise HTTPException(404, f"Memory item {item_id} not found")
        return receipt

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
        from mochi.db import update_memory_item

        body = await request.json()
        uid = OWNER_USER_ID or 0
        content = body.get("content", "").strip()
        importance = max(1, min(3, int(body.get("importance", 1))))

        if not update_memory_item(
            item_id,
            uid,
            content=content,
            importance=importance,
        ):
            raise HTTPException(404, f"Memory item {item_id} not found")
        log.info("Admin: updated memory item #%d imp=%d", item_id, importance)
        return {"ok": True, "id": item_id}

    # ═══════════════════════════════════════════════════════════════════════
    # Diagnostics
    # ═══════════════════════════════════════════════════════════════════════

    @app.get("/api/diagnostics/errors", dependencies=[Depends(_verify_token)])
    async def api_diagnostics_errors():
        """Return recent WARNING+ log entries from the in-memory buffer."""
        try:
            from mochi.error_buffer import get_recent_errors
            errors = get_recent_errors(hours=24)
            return {"ok": True, "errors": errors, "count": len(errors)}
        except ImportError:
            return {"ok": True, "errors": [], "count": 0}

    @app.get("/api/models/health", dependencies=[Depends(_verify_token)])
    async def api_models_health():
        """Return per-tier model health statistics."""
        try:
            from mochi.model_health import get_health
            return {"ok": True, "tiers": get_health()}
        except ImportError:
            return {"ok": True, "tiers": {}}

    @app.get("/api/diagnostics/export", dependencies=[Depends(_verify_token)])
    async def api_diagnostics_export():
        """Generate full diagnostic report as downloadable text file."""
        try:
            from mochi.error_buffer import get_diagnostic_report
            report = get_diagnostic_report()
        except Exception as e:
            report = sanitize_persistent_text(
                f"Failed to generate diagnostic report: {e}"
            )
        from datetime import datetime as _dt
        stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
        return StarletteResponse(
            content=report,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="mochibot-diagnostics-{stamp}.txt"',
            },
        )

    # ── Checkup (lightweight health report) ────────────────────────────
    @app.get("/api/checkup", dependencies=[Depends(_verify_token)])
    async def api_checkup():
        """Lightweight system health check — prompt size, DB, memory, runtime."""
        try:
            from mochi.checkup_core import run_checkup
            from mochi.config import OWNER_USER_ID
            data = run_checkup(OWNER_USER_ID)
            return {"ok": True, **data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Chat migration routes (搬家) ────────────────────────────────
    try:
        from mochi.admin.migration_routes import register_migration_routes
        register_migration_routes(app, _verify_token)
    except ImportError:
        pass


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
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    except SystemExit as exc:
        if exc.code == 1:
            log.error("Admin portal could not bind to %s:%d", bind, port)
            return
        raise
    except OSError as e:
        if "address" in str(e).lower() or getattr(e, "errno", 0) in (98, 10048):
            log.warning("Admin portal 端口 %d 被占用，跳过启动 (bot 不受影响)", port)
            return
        raise
