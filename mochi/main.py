"""MochiBot — main entry point.

Starts all subsystems:
1. Database initialization
2. Skill discovery
3. Transport (Telegram by default)
4. Heartbeat loop (includes maintenance scheduling)
"""

import asyncio
import logging

from mochi.config import (
    TELEGRAM_BOT_TOKEN,
    OWNER_USER_ID,
)
from mochi.db import init_db
import mochi.skills as skill_registry
from mochi.ai_client import chat, ChatResult
from mochi.transport import IncomingMessage
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

    # 1. Database
    init_db()
    log.info("Database ready")

    # 2. Skills
    skills = skill_registry.discover()
    log.info("Skills loaded: %s", skills)

    # 2b. Observers
    from mochi.observers import discover as discover_observers
    observers = discover_observers()
    log.info("Observers loaded: %s", observers)

    # 3. Transport
    from mochi.transport.telegram import TelegramTransport, set_message_handler
    transport = TelegramTransport()
    set_message_handler(handle_message)

    # 4. Heartbeat — wire up send callback
    async def send_proactive(user_id: int, text: str):
        await transport.send_message(user_id, text)

    set_send_callback(send_proactive)

    # 5. Start transport
    await transport.start()
    log.info("Transport started: %s", transport.name)

    # 5b. Admin portal
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

    # Reminder timer: precise delivery at exact remind_at times
    async def _send_via_transport(user_id: int, text: str) -> None:
        await transport.send_message(user_id, text)
    set_reminder_callback(_send_via_transport)
    asyncio.create_task(reminder_loop())
    log.info("Heartbeat and reminder timer started")

    log.info("=" * 50)
    log.info("MochiBot is alive! 🍡")
    log.info("=" * 50)

    # Keep running
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down...")
        await transport.stop()


if __name__ == "__main__":
    asyncio.run(main())
