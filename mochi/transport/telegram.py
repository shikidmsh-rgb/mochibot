"""Telegram transport — sends and receives messages via Telegram Bot API.

This is the default transport. Requires TELEGRAM_BOT_TOKEN in .env.
"""

import asyncio
import logging
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

from mochi.transport import Transport, IncomingMessage
from mochi.transport.utils import split_bubbles as _split_bubbles_util
from mochi.config import (
    TELEGRAM_BOT_TOKEN, OWNER_USER_ID, set_owner_user_id,
    TG_BUBBLE_DELAY_S, TG_BUBBLE_MAX, TG_BUBBLE_DELIMITER,
    TG_BUBBLE_MIN_CHARS,
)

log = logging.getLogger(__name__)


def _split_bubbles(text: str, max_bubbles: int = 4,
                   delimiter: str = "|||",
                   min_chars: int = 8) -> list[str]:
    return _split_bubbles_util(text, max_bubbles, delimiter, min_chars)


def _is_owner(user_id: int) -> bool:
    """Check if user_id matches the configured owner."""
    from mochi.config import OWNER_USER_ID as current_owner
    return current_owner and user_id == current_owner

# Message handler callback — set by main.py during initialization
_on_message_callback = None


def set_message_handler(callback) -> None:
    """Register the function to handle incoming messages.

    Signature: async def callback(msg: IncomingMessage) -> ChatResult
    Returns a ChatResult with text reply and optional sticker file_ids.
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
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("heartbeat", self._cmd_heartbeat))
        self._app.add_handler(CommandHandler("status", self._cmd_heartbeat))
        self._app.add_handler(CommandHandler("cost", self._cmd_cost))
        self._app.add_handler(CommandHandler("notes", self._cmd_notes))
        self._app.add_handler(CommandHandler("diary", self._cmd_diary))
        self._app.add_handler(CommandHandler("restart", self._cmd_restart))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        self._app.add_handler(
            MessageHandler(filters.Sticker.ALL, self._handle_sticker)
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
            bubbles = _split_bubbles(text, TG_BUBBLE_MAX, TG_BUBBLE_DELIMITER, TG_BUBBLE_MIN_CHARS)
            for i, bubble in enumerate(bubbles):
                if i > 0:
                    await self._app.bot.send_chat_action(
                        chat_id=user_id, action="typing",
                    )
                    await asyncio.sleep(TG_BUBBLE_DELAY_S)
                # Respect Telegram 4096 char limit per message
                for start in range(0, len(bubble), 4096):
                    await self._app.bot.send_message(
                        chat_id=user_id,
                        text=bubble[start:start + 4096],
                    )
        except Exception as e:
            log.error("Failed to send Telegram message: %s", e)

    async def send_sticker(self, chat_id: int, file_id: str) -> None:
        """Send a Telegram sticker by file_id."""
        if not self._app:
            return
        try:
            await self._app.bot.send_sticker(chat_id=chat_id, sticker=file_id)
        except Exception as e:
            log.error("Failed to send sticker: %s", e)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "我是你的 AI 伙伴，会记住我们的对话，在需要时提醒你。\n\n"
            "直接跟我聊天就行，不用特殊格式。\n\n"
            "指令：\n"
            "/help — 显示本帮助\n"
            "/heartbeat — 心跳状态\n"
            "/cost — Token 用量统计\n"
            "/notes — 查看笔记\n"
            "/diary — 查看今日日記\n"
            "/restart — 重启 Bot"
        )

    async def _cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        await update.message.reply_text("正在重启...")
        from mochi.shutdown import request_restart
        request_restart(update.effective_chat.id)

    async def _cmd_heartbeat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        from mochi.heartbeat import get_stats
        from mochi.db import get_last_heartbeat_log
        stats = get_stats()
        entry = get_last_heartbeat_log()

        lines = [
            "📊 系统状态",
            "",
            f"状态: {stats['state']}",
            f"今日主动推送: {stats['proactive_today']}/{stats['proactive_limit']}",
            f"上次思考: {stats['last_think_at'] or '无'}",
        ]

        if entry:
            summary = entry.get("summary") or "(无)"
            if len(summary) > 600:
                summary = summary[:600] + "…(截断)"
            lines += [
                "",
                "── 最近一次心跳 ──",
                f"时间: {entry.get('created_at', '?')}",
                f"状态: {entry.get('state', '?')}  |  动作: {entry.get('action', '(无)')}",
                "",
                summary,
            ]

        await update.message.reply_text("\n".join(lines))

    async def _cmd_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        from mochi.db import get_usage_summary
        s = get_usage_summary()

        def _format_block(title: str, by_model: dict) -> list[str]:
            lines = [title]
            if not by_model:
                lines.append("  (无记录)")
                return lines
            for model, data in sorted(by_model.items()):
                lines.append(f"  {model}")
                lines.append(f"    input {data['prompt']:,}  |  output {data['completion']:,}")
            return lines

        lines = _format_block("📊 今日", s["today"]["by_model"])
        lines.append("")
        lines += _format_block("📊 本月", s["month"]["by_model"])

        await update.message.reply_text("\n".join(lines))

    async def _cmd_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        from pathlib import Path
        notes_path = Path(__file__).resolve().parent.parent.parent / "data" / "notes.md"
        notes = []
        if notes_path.exists():
            for line in notes_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("- "):
                    notes.append(stripped[2:])
        if not notes:
            await update.message.reply_text("No notes.")
            return
        lines = ["📝 Notes"] + [f"{i+1}. {n}" for i, n in enumerate(notes)]
        await update.message.reply_text("\n".join(lines))

    async def _cmd_diary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        from mochi.diary import diary
        from mochi.config import logical_today
        status = diary.read(section="今日状態") or "(无)"
        journal = diary.read(section="今日日記") or "(无)"
        today = logical_today()
        text = (
            f"📖 今日日記 ({today})\n\n"
            f"── 今日状態 ──\n{status}\n\n"
            f"── 今日日記 ──\n{journal}"
        )
        await update.message.reply_text(text)

    # ── Handlers ──────────────────────────────────────────────

    async def _check_owner(self, update: Update) -> int | None:
        """Validate owner and return user_id, or reply with rejection and return None."""
        user_id = update.effective_user.id
        from mochi.config import OWNER_USER_ID as _current_owner
        if not _current_owner:
            set_owner_user_id(user_id)
            log.info("Owner auto-detected: user_id=%d", user_id)
        elif user_id != _current_owner:
            await update.message.reply_text("Sorry, I'm a personal companion bot.")
            return None
        return user_id

    @staticmethod
    def _dispatch_state_signals() -> None:
        """Dispatch heartbeat state transitions on user activity."""
        from mochi.heartbeat import should_wake_on_message, wake_up, clear_morning_hold, clear_silent_pause
        if should_wake_on_message():
            wake_up("user_message")
        clear_morning_hold()
        clear_silent_pause()

    async def _send_chat_result(self, chat_id: int, result) -> None:
        """Send a ChatResult — text message + any pending stickers."""
        if result.text:
            await self.send_message(chat_id, result.text)
        for file_id in result.stickers:
            await self.send_sticker(chat_id, file_id)
            from mochi.skills.sticker.handler import record_last_sent_sticker
            record_last_sent_sticker(chat_id, file_id)

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return

        user_id = await self._check_owner(update)
        if user_id is None:
            return

        self._dispatch_state_signals()

        msg = IncomingMessage(
            user_id=user_id,
            channel_id=update.effective_chat.id,
            text=update.message.text,
            transport="telegram",
        )

        if _on_message_callback:
            result = await _on_message_callback(msg)
            if result:
                await self._send_chat_result(update.effective_chat.id, result)
            # Check for goodnight keywords AFTER Chat has replied
            from mochi.heartbeat import check_sleep_entry, handle_sleep_keyword
            if check_sleep_entry(update.message.text):
                await handle_sleep_keyword(user_id)
        else:
            await update.message.reply_text("I'm still waking up... try again in a moment.")

    async def _handle_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.sticker:
            return

        user_id = await self._check_owner(update)
        if user_id is None:
            return

        self._dispatch_state_signals()

        sticker = update.message.sticker
        msg = IncomingMessage(
            user_id=user_id,
            channel_id=update.effective_chat.id,
            text=update.message.caption or "",
            transport="telegram",
            raw={
                "sticker": {
                    "file_id": sticker.file_id,
                    "emoji": sticker.emoji or "",
                    "set_name": sticker.set_name or "",
                },
            },
        )

        if _on_message_callback:
            result = await _on_message_callback(msg)
            if result:
                await self._send_chat_result(update.effective_chat.id, result)
        else:
            await update.message.reply_text("I'm still waking up... try again in a moment.")
