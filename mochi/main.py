"""MochiBot — main entry point.

Starts all subsystems:
1. Database initialization
2. Skill discovery
3. Transport — Telegram or WeChat (one at a time)
4. Heartbeat loop (includes maintenance scheduling)
"""

import asyncio
import logging

from mochi.config import (
    TELEGRAM_BOT_TOKEN,
    OWNER_USER_ID,
    WEIXIN_ENABLED,
    validate_config,
)
from mochi.db import init_db
import mochi.skills as skill_registry
from mochi.ai_client import chat, ChatResult
from mochi.transport import Transport, IncomingMessage
from mochi.heartbeat import heartbeat_loop, set_send_callback
from mochi.reminder_timer import reminder_loop, set_send_callback as set_reminder_callback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mochi")


async def handle_message(msg: IncomingMessage) -> ChatResult:
    """Central message handler — called by all transports."""
    return await chat(msg)


async def main():
    """Boot sequence."""
    log.info("=" * 50)
    log.info("MochiBot starting up...")
    log.info("=" * 50)

    # 0. Database (before config validation — tier models live in DB)
    init_db()
    log.info("Database ready")

    # 0b. Seed model config from .env on first run (DB empty)
    from mochi.admin.admin_db import seed_models_from_env
    seed_models_from_env()

    # 1. Config validation
    validate_config()

    # 2. Skills
    skills = skill_registry.discover()
    log.info("Skills loaded: %s", skills)

    # 2b. Observers
    from mochi.observers import discover as discover_observers
    observers = discover_observers()
    log.info("Observers loaded: %s", observers)

    # 3. Transport — only one active at a time
    transport: Transport | None = None

    if TELEGRAM_BOT_TOKEN:
        if WEIXIN_ENABLED:
            log.warning("Both Telegram and WeChat configured — using Telegram. "
                        "Disable one in .env or admin portal to silence this warning.")
        from mochi.transport.telegram import TelegramTransport, set_message_handler
        transport = TelegramTransport()
        set_message_handler(handle_message)
    elif WEIXIN_ENABLED:
        from mochi.transport.weixin import WeixinTransport
        from mochi.transport.weixin import set_message_handler as set_weixin_handler
        transport = WeixinTransport()
        set_weixin_handler(handle_message)

    if transport:
        await transport.start()
        log.info("Transport started: %s", transport.name)

    # 4. Heartbeat — wire up send callback to transport
    if transport:
        _t = transport  # capture for closure

        async def send_proactive(user_id: int, text: str):
            await _t.send_message(user_id, text)

        set_send_callback(send_proactive)
        set_reminder_callback(send_proactive)

    # 5. Admin portal
    from mochi.config import ADMIN_ENABLED, ADMIN_PORT, ADMIN_BIND
    if ADMIN_ENABLED:
        try:
            from mochi.admin.admin_server import start_admin_server
            asyncio.create_task(start_admin_server(ADMIN_PORT, ADMIN_BIND))
            log.info("Admin portal: http://%s:%d", ADMIN_BIND, ADMIN_PORT)
        except ImportError:
            log.warning("Admin portal disabled: pip install fastapi uvicorn")
        except Exception as e:
            log.warning("Admin portal failed to start: %s", e)

    # 6. Start background tasks
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(reminder_loop())
    log.info("Heartbeat and reminder timer started")

    log.info("=" * 50)
    log.info("MochiBot is alive!")
    log.info("=" * 50)

    # Keep running
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down...")
        if transport:
            await transport.stop()


if __name__ == "__main__":
    asyncio.run(main())
