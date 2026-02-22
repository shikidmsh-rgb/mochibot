"""MochiBot — main entry point.

Starts all subsystems:
1. Database initialization
2. Skill discovery
3. Transport (Telegram by default)
4. Heartbeat loop
5. Maintenance scheduler
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from mochi.config import (
    TELEGRAM_BOT_TOKEN,
    MAINTENANCE_HOUR,
    TIMEZONE_OFFSET_HOURS,
    OWNER_USER_ID,
)
from mochi.db import init_db, get_pending_reminders, mark_reminder_fired
import mochi.skills as skill_registry
from mochi.ai_client import chat
from mochi.transport import IncomingMessage
from mochi.transport.telegram import TelegramTransport, set_message_handler
from mochi.heartbeat import heartbeat_loop, set_send_callback
from mochi.memory_engine import smart_maintenance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mochi")

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))


async def handle_message(msg: IncomingMessage) -> str:
    """Central message handler — called by all transports."""
    return await chat(msg)


async def maintenance_scheduler():
    """Run smart maintenance at the configured hour daily."""
    while True:
        now = datetime.now(TZ)
        # Calculate seconds until next maintenance hour
        target = now.replace(hour=MAINTENANCE_HOUR, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        log.info("Next maintenance in %.0f minutes", wait_seconds / 60)
        await asyncio.sleep(wait_seconds)

        log.info("Running scheduled maintenance...")
        try:
            results = smart_maintenance()
            log.info("Maintenance results: %s", results)
        except Exception as e:
            log.error("Maintenance failed: %s", e, exc_info=True)


async def reminder_checker(transport):
    """Check for due reminders every 60 seconds and deliver them."""
    while True:
        try:
            reminders = get_pending_reminders()
            for r in reminders:
                try:
                    await transport.send_message(
                        r["channel_id"],
                        f"⏰ Reminder: {r['message']}",
                    )
                    mark_reminder_fired(r["id"])
                    log.info("Reminder #%d fired", r["id"])
                except Exception as e:
                    log.error("Failed to fire reminder #%d: %s", r["id"], e)
        except Exception as e:
            log.error("Reminder checker error: %s", e, exc_info=True)
        await asyncio.sleep(60)


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
    transport = TelegramTransport()
    set_message_handler(handle_message)

    # 4. Heartbeat — wire up send callback
    async def send_proactive(user_id: int, text: str):
        await transport.send_message(user_id, text)

    set_send_callback(send_proactive)

    # 5. Start transport
    await transport.start()
    log.info("Transport started: %s", transport.name)

    # 6. Start background tasks
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(maintenance_scheduler())
    asyncio.create_task(reminder_checker(transport))
    log.info("Heartbeat, maintenance, and reminder checker started")

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
