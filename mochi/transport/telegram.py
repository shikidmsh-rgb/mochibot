"""Telegram transport — sends and receives messages via Telegram Bot API.

This is the default transport. Requires TELEGRAM_BOT_TOKEN in .env.
"""

import logging
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

from mochi.transport import Transport, IncomingMessage
from mochi.config import TELEGRAM_BOT_TOKEN, OWNER_USER_ID, set_owner_user_id

log = logging.getLogger(__name__)


def _is_owner(user_id: int) -> bool:
    """Check if user_id matches the configured owner."""
    from mochi.config import OWNER_USER_ID as current_owner
    return current_owner and user_id == current_owner

# Message handler callback — set by main.py during initialization
_on_message_callback = None


def set_message_handler(callback) -> None:
    """Register the function to handle incoming messages.

    Signature: async def callback(msg: IncomingMessage) -> str
    Returns the bot's response text.
    """
    global _on_message_callback
    _on_message_callback = callback


class TelegramTransport(Transport):
    """Telegram Bot API transport."""

    def __init__(self):
        self._app: Application | None = None

    @property
    def name(self) -> str:
        return "telegram"

    async def start(self) -> None:
        if not TELEGRAM_BOT_TOKEN:
            log.warning("TELEGRAM_BOT_TOKEN not set, Telegram transport disabled")
            return

        self._app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Register handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("cost", self._cmd_cost))
        self._app.add_handler(CommandHandler("heartbeat", self._cmd_heartbeat))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram transport started")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            log.info("Telegram transport stopped")

    async def send_message(self, user_id: int, text: str) -> None:
        if not self._app:
            log.warning("Telegram not started, cannot send message")
            return
        try:
            # Split long messages (Telegram limit: 4096 chars)
            for i in range(0, len(text), 4096):
                await self._app.bot.send_message(
                    chat_id=user_id,
                    text=text[i:i + 4096],
                )
        except Exception as e:
            log.error("Failed to send Telegram message: %s", e)

    # ── Handlers ──────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "Hey! I'm your MochiBot companion. Just talk to me like you would a friend. 🍡"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "I'm an AI companion that remembers our conversations and checks in on you.\n\n"
            "Just chat naturally — I'll remember important things and remind you when needed.\n\n"
            "Commands:\n"
            "/start — Say hi\n"
            "/help — This message\n"
            "/status — Check my heartbeat status\n"
            "/heartbeat — Last heartbeat time and output\n"
            "/cost — LLM token usage summary"
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        from mochi.heartbeat import get_stats
        stats = get_stats()
        await update.message.reply_text(
            f"State: {stats['state']}\n"
            f"Proactive today: {stats['proactive_today']}/{stats['proactive_limit']}\n"
            f"Last think: {stats['last_think_at'] or 'never'}"
        )

    async def _cmd_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        from mochi.db import get_usage_summary
        s = get_usage_summary()
        t, m = s["today"], s["month"]

        lines = [
            "📊 LLM Usage",
            "",
            f"Today: {t['total']:,} tokens ({t['calls']} calls)",
            f"  ├ prompt: {t['prompt']:,}",
            f"  └ completion: {t['completion']:,}",
            "",
            f"This month: {m['total']:,} tokens ({m['calls']} calls)",
            f"  ├ prompt: {m['prompt']:,}",
            f"  └ completion: {m['completion']:,}",
        ]

        if s["by_model"]:
            lines.append("")
            lines.append("By model (this month):")
            for model, data in sorted(s["by_model"].items(), key=lambda x: -x[1]["total"]):
                lines.append(f"  {model}: {data['total']:,} tokens ({data['calls']} calls)")
            if len(s["by_model"]) > 1:
                lines.append("  ↑ multiple models = you switched config at some point")

        if s["by_purpose"]:
            lines.append("")
            lines.append("By purpose (this month):")
            for purpose, tokens in sorted(s["by_purpose"].items(), key=lambda x: -x[1]):
                lines.append(f"  {purpose}: {tokens:,} tokens")

        await update.message.reply_text("\n".join(lines))

    async def _cmd_heartbeat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show last heartbeat time and output."""
        if not _is_owner(update.effective_user.id):
            return
        from mochi.db import get_last_heartbeat_log
        entry = get_last_heartbeat_log()
        if not entry:
            await update.message.reply_text("💓 No heartbeat log found yet.")
            return

        summary = entry.get("summary") or "(none)"
        state = entry.get("state") or "?"
        action = entry.get("action") or "(none)"
        created_at = entry.get("created_at") or "?"

        if len(summary) > 800:
            summary = summary[:800] + "...(truncated)"

        lines = [
            f"💓 Last Heartbeat",
            f"Time: {created_at}",
            f"State: {state}  |  Action: {action}",
            "",
            f"Summary:",
            summary,
        ]
        await update.message.reply_text("\n".join(lines))

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return

        user_id = update.effective_user.id

        # Auto-detect owner: first user to message becomes the owner
        from mochi.config import OWNER_USER_ID as _current_owner
        if not _current_owner:
            set_owner_user_id(user_id)
            log.info("Owner auto-detected: user_id=%d", user_id)
        elif user_id != _current_owner:
            await update.message.reply_text("Sorry, I'm a personal companion bot.")
            return

        # Wake heartbeat only after owner auth passes
        from mochi.heartbeat import force_wake
        force_wake()

        msg = IncomingMessage(
            user_id=user_id,
            channel_id=update.effective_chat.id,
            text=update.message.text,
            transport="telegram",
        )

        if _on_message_callback:
            response = await _on_message_callback(msg)
            if response:
                await self.send_message(update.effective_chat.id, response)
        else:
            await update.message.reply_text("I'm still waking up... try again in a moment.")
