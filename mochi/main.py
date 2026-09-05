"""MochiBot — main entry point.

Starts all subsystems:
1. Database initialization
2. Skill discovery
3. Transport — Telegram or WeChat (one at a time)
4. Heartbeat loop (includes maintenance scheduling)
"""

import asyncio
import logging
import os
import signal
import socket
import sys

from mochi.admin_access import build_admin_base_url, configure_safe_logging
from mochi.config import (
    TELEGRAM_BOT_TOKEN,
    OWNER_USER_ID,
    WEIXIN_ENABLED,
    WEIXIN_BOT_TOKEN,
    LOG_LEVEL,
    validate_config,
)
from mochi.db import init_db
import mochi.skills as skill_registry
from mochi.ai_client import chat, ChatResult
from mochi.transport import Transport, IncomingMessage
from mochi.heartbeat import (
    heartbeat_loop,
    reload_state_after_config_seed,
    set_bedtime_callback,
    set_main_runtime_callbacks,
    set_weekly_callback,
)
from mochi.main_runtime import MainRuntimeEntry
from mochi.reminder_timer import (
    reminder_loop,
    set_self_reminder_callbacks,
    set_send_callback as set_reminder_callback,
)
from mochi.shutdown import (
    consume_restart_flag,
    init_restart_event,
    is_agent_enabled,
    set_agent_enabled,
    RESTART_EXIT_CODE,
)

configure_safe_logging(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format_string="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    date_format="%H:%M:%S",
)
from mochi.error_buffer import BufferHandler  # noqa: E402
logging.getLogger("mochi").addHandler(BufferHandler())
log = logging.getLogger("mochi")


# Module-level flag — set in main(), read by handle_message()
_setup_mode = False


def _admin_port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _log_admin_startup(bind: str, port: int, token: str) -> None:
    log.info("Admin portal: %s", build_admin_base_url(bind, port))
    if token:
        log.info("Admin token protection enabled")


async def handle_message(msg: IncomingMessage) -> ChatResult:
    """Central message handler — called by all transports."""
    if _setup_mode:
        return ChatResult(
            text="我还在准备中～发 /admin 获取管理后台链接，完成配置后就能正常聊天了"
        )
    return await chat(msg)


async def main():
    """Boot sequence."""
    log.info("=" * 50)
    log.info("MochiBot starting up...")
    log.info("=" * 50)

    # 0. Database (before config validation — tier models live in DB)
    init_db()
    from mochi.db import recover_interrupted_scheduled_runs
    recover_interrupted_scheduled_runs()
    log.info("Database ready")

    # Prepare the canonical file-backed Core before any runtime reads it.
    from mochi.core_store import initialize_core
    core_status = initialize_core(OWNER_USER_ID or 0)
    log.info("Core ready (%s)", core_status.get("status", "existing"))

    # 0b. Seed model config from .env on first run (DB empty)
    from mochi.admin.admin_db import seed_models_from_env
    seed_models_from_env()

    # 0c. Seed system config from .env on first run (DB empty)
    from mochi.admin.admin_db import seed_system_config_from_env
    seed_system_config_from_env()
    reload_state_after_config_seed()

    # 1. Config validation
    config_status = validate_config()
    global _setup_mode
    _setup_mode = config_status != "ok"
    agent_enabled = is_agent_enabled()
    if _setup_mode:
        agent_enabled = False
        try:
            set_agent_enabled(False)
        except OSError as exc:
            log.error("Could not persist disabled Agent state: %s", exc)
    from mochi.config import ADMIN_ENABLED, ADMIN_PORT, ADMIN_BIND, ADMIN_TOKEN
    if ADMIN_ENABLED and not _admin_port_available(ADMIN_BIND, ADMIN_PORT):
        log.critical(
            "Admin port %d is already in use. Another MochiBot may be running; "
            "stop it or set ADMIN_PORT to a different value.",
            ADMIN_PORT,
        )
        return

    # 2. Skills
    skills = skill_registry.discover()
    skill_registry.init_all_skill_schemas()
    log.info("Skills loaded: %s", skills)

    # 2a. Register diagnostic providers (after skill schemas are initialized)
    try:
        from mochi.error_buffer import register_diagnostic_provider
        from mochi.skills.reminder.queries import get_reminder_diagnostic_section
        register_diagnostic_provider("reminder_state", get_reminder_diagnostic_section)
    except Exception as e:
        log.warning("Could not register reminder diagnostic provider: %s", e)

    # 2b. Observers
    from mochi.observers import discover as discover_observers
    observers = discover_observers()
    log.info("Observers loaded: %s", observers)

    # 3. Transport — only one active at a time
    transport: Transport | None = None

    if (_setup_mode or agent_enabled) and TELEGRAM_BOT_TOKEN:
        if WEIXIN_ENABLED:
            log.warning("Both Telegram and WeChat configured — using Telegram. "
                        "Disable one in .env or admin portal to silence this warning.")
        from mochi.transport.telegram import TelegramTransport, set_message_handler
        transport = TelegramTransport()
        set_message_handler(handle_message)
    elif (_setup_mode or agent_enabled) and WEIXIN_ENABLED and WEIXIN_BOT_TOKEN:
        from mochi.transport.weixin import WeixinTransport
        from mochi.transport.weixin import set_message_handler as set_weixin_handler
        transport = WeixinTransport()
        set_weixin_handler(handle_message)

    # 3a. Restore owner ID before polling can accept an inbound sender.
    if transport and hasattr(transport, "restore_owner_id"):
        from mochi.db import get_skill_config
        saved_id = get_skill_config("_transport:wechat").get("owner_weixin_id")
        if saved_id:
            transport.restore_owner_id(saved_id, source="DB")

    if transport:
        await transport.start()
        log.info("Transport started: %s", transport.name)

    # 3b. Send restart-complete notification if restarting
    restart_info = consume_restart_flag()
    if restart_info and transport:
        # Restore transport-specific state so send_message works immediately
        weixin_id = restart_info.get("weixin_id")
        if weixin_id and hasattr(transport, "restore_owner_id"):
            transport.restore_owner_id(weixin_id)
        try:
            await transport.send_message(
                restart_info["channel_id"], "重启完成 ✅")
            log.info("Sent restart-complete notification to channel %s",
                     restart_info["channel_id"])
        except Exception as e:
            log.warning("Failed to send restart-complete notification: %s", e)

    # 4. Heartbeat — wire up send callback to transport
    if transport:
        _t = transport  # capture for closure

        async def send_proactive(user_id: int, text: str) -> bool:
            return await _t.send_message(user_id, text)

        async def enter_bedtime(user_id: int, trigger: str) -> bool:
            entry = MainRuntimeEntry.bedtime(
                trigger=trigger,
                user_id=user_id,
                channel_id=user_id,
                transport=_t.name,
            )
            result = await chat(runtime_entry=entry)
            if result.disposition == "skip":
                return True
            if not result.text and not result.stickers:
                return False
            delivered = await _t.send_chat_result(user_id, result)
            if delivered:
                result.confirm_delivered()
            return delivered

        async def enter_weekly(
            user_id: int,
            logical_date: str,
            period_key: str,
        ) -> None:
            entry = MainRuntimeEntry.weekly_maintenance(
                logical_date=logical_date,
                period_key=period_key,
                user_id=user_id,
                channel_id=user_id,
                transport=_t.name,
            )
            await chat(runtime_entry=entry)

        async def prepare_self_reminder(entry: MainRuntimeEntry) -> ChatResult:
            return await chat(runtime_entry=entry)

        async def deliver_self_reminder(
            channel_id: int,
            result: ChatResult,
        ) -> bool:
            return await _t.send_chat_result(channel_id, result)

        set_main_runtime_callbacks(
            prepare_self_reminder,
            deliver_self_reminder,
            _t.name,
        )
        set_bedtime_callback(enter_bedtime)
        set_weekly_callback(enter_weekly)
        set_reminder_callback(send_proactive)
        set_self_reminder_callbacks(
            prepare_self_reminder,
            deliver_self_reminder,
            _t.name,
        )

    # 5. Start runtime work only when chat can actually run.
    runtime_tasks: list[asyncio.Task] = []
    if agent_enabled and not _setup_mode and transport:
        runtime_tasks = [
            asyncio.create_task(heartbeat_loop()),
            asyncio.create_task(reminder_loop()),
        ]
        from mochi.conversation_summary import resume_conversation_summaries
        from mochi.memory_extraction import resume_memory_extractions
        resume_conversation_summaries()
        resume_memory_extractions()
        log.info("Heartbeat, reminder timer, and memory recovery started")
    else:
        log.info("Background runtime skipped until model and transport are configured")

    runtime_running = bool(agent_enabled and not _setup_mode and transport)

    async def stop_runtime() -> bool:
        nonlocal runtime_running
        if not runtime_running and not transport:
            return False
        for task in runtime_tasks:
            task.cancel()
        if runtime_tasks:
            await asyncio.gather(*runtime_tasks, return_exceptions=True)
        if transport:
            await transport.stop()
        runtime_running = False
        return True

    def runtime_status() -> dict:
        return {
            "running": runtime_running,
            "enabled": agent_enabled,
            "pid": os.getpid(),
            "transport": transport.name if transport and runtime_running else "",
            "setup_mode": _setup_mode,
            "weixin_session_expired": bool(
                transport and getattr(transport, "_session_expired", False)
            ),
        }

    # 6. Admin portal shares this process with the bot runtime.
    if ADMIN_ENABLED:
        try:
            from mochi.admin.admin_server import (
                register_runtime_controls,
                start_admin_server,
            )
            register_runtime_controls(runtime_status)
            asyncio.create_task(start_admin_server(ADMIN_PORT, ADMIN_BIND))
            _log_admin_startup(ADMIN_BIND, ADMIN_PORT, ADMIN_TOKEN)
        except ImportError:
            log.warning("Admin portal dependencies missing; run setup again")
        except Exception as exc:
            log.warning("Admin portal failed to start: %s", exc)

    if config_status == "setup_mode":
        log.info("=" * 55)
        log.info("  SETUP MODE active")
        log.info("  Send /admin to the bot to get the admin portal URL")
        log.info("=" * 55)
    elif config_status == "admin_only":
        log.info("=" * 55)
        log.info("  ADMIN-ONLY MODE active")
        log.info("  Open the admin portal to configure a model and transport")
        log.info("=" * 55)
    elif not agent_enabled:
        log.info("=" * 50)
        log.info("Agent is stopped; Admin Portal remains available")
        log.info("=" * 50)
    else:
        log.info("=" * 50)
        log.info("MochiBot is alive!")
        log.info("=" * 50)

    # Keep running — also watch for restart signal
    restart_event = init_restart_event()

    # Register SIGBREAK handler for graceful shutdown on Windows.
    # Admin portal sends CTRL_BREAK_EVENT to request a clean restart;
    # the handler sets restart_event so the loop below picks it up.
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        loop = asyncio.get_running_loop()

        def _on_break(signum, frame):
            log.info("Received SIGBREAK — initiating graceful shutdown")
            loop.call_soon_threadsafe(restart_event.set)

        signal.signal(signal.SIGBREAK, _on_break)

    try:
        while True:
            sleep_task = asyncio.create_task(asyncio.sleep(3600))
            restart_task = asyncio.create_task(restart_event.wait())
            done, pending = await asyncio.wait(
                {sleep_task, restart_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if restart_event.is_set():
                log.info("Restart requested — shutting down (exit code %d)",
                         RESTART_EXIT_CODE)
                await stop_runtime()
                sys.exit(RESTART_EXIT_CODE)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        await stop_runtime()
    except SystemExit:
        raise  # preserve exit code (42 = restart)


if __name__ == "__main__":
    asyncio.run(main())
