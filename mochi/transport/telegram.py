"""Telegram transport — sends and receives messages via Telegram Bot API.

This is the default transport. Requires TELEGRAM_BOT_TOKEN in .env.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from telegram import Update, ReactionTypeEmoji
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
    TG_STATUS_REACTIONS_ENABLED, TG_STATUS_MESSAGE_ENABLED,
    TG_STATUS_EDIT_INTERVAL_S,
)

log = logging.getLogger(__name__)


# ── Tool Status UX ───────────────────────────────────────────

_TOOL_STATUS_LABELS: dict[str, str] = {
    "web_search": "正在搜索…",
    "recall_memory": "正在回忆…",
    "save_memory": "正在记录…",
    "update_core_memory": "正在更新记忆…",
    "list_memories": "正在查记忆…",
    "manage_note": "正在整理笔记…",
    "manage_todo": "正在整理待办…",
    "checkin_habit": "正在打卡…",
    "query_habit": "正在查习惯…",
    "edit_habit": "正在编辑习惯…",
    "get_oura_data": "正在查 Oura…",
    "log_meal": "正在记录饮食…",
    "query_meals": "正在查饮食…",
    "manage_reminder": "正在设置提醒…",
    "get_weather": "正在查天气…",
    "run_checkup": "正在检查…",
    "send_sticker": "正在选贴纸…",
    "request_tools": "正在加载工具…",
}
_TOOL_STATUS_DEFAULT = "正在处理…"


def _tool_label(tool_name: str | None) -> str:
    if not tool_name:
        return "思考中…"
    return _TOOL_STATUS_LABELS.get(tool_name, _TOOL_STATUS_DEFAULT)


@dataclass
class _StatusState:
    """Per-request tool-call status message and reaction state."""
    status_msg_id: int | None = None
    last_edit_time: float = 0.0
    last_label: str = ""
    reaction_state: str = ""  # "" | "working" | "done"


async def _set_reaction(bot, chat_id: int, message_id: int, emoji: str | None) -> None:
    """Set or clear an emoji reaction on a message. Silently ignores all errors."""
    try:
        if emoji is None:
            await bot.set_message_reaction(chat_id, message_id, reaction=[])
        else:
            await bot.set_message_reaction(
                chat_id, message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
    except Exception:
        pass


def _split_bubbles(text: str, max_bubbles: int = 8,
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
        self._app.add_handler(CommandHandler("cost", self._cmd_cost))
        self._app.add_handler(CommandHandler("notes", self._cmd_notes))
        self._app.add_handler(CommandHandler("diary", self._cmd_diary))
        self._app.add_handler(CommandHandler("restart", self._cmd_restart))
        self._app.add_handler(CommandHandler("admin", self._cmd_admin))
        self._app.add_handler(CommandHandler("skilloff", self._cmd_skilloff))
        self._app.add_handler(CommandHandler("skillon", self._cmd_skillon))
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
            "/admin — 管理后台\n"
            "/skilloff — 闲聊模式（省 token）\n"
            "/skillon — 恢复完整模式\n"
            "/restart — 重启 Bot"
        )

    async def _cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        await update.message.reply_text("正在重启...")
        from mochi.shutdown import request_restart
        request_restart(update.effective_chat.id)

    async def _cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Use _check_owner so the first user auto-becomes owner in setup mode
        user_id = await self._check_owner(update)
        if user_id is None:
            return
        from mochi.config import ADMIN_PORT, ADMIN_BIND, ADMIN_TOKEN, _detect_host_ip
        # /admin is always sent from a remote device (phone), so use LAN IP
        host = _detect_host_ip() or ADMIN_BIND
        if host in ("0.0.0.0", "127.0.0.1", "localhost", "::1"):
            host = "<your-server-ip>"
        url = f"http://{host}:{ADMIN_PORT}"
        if ADMIN_TOKEN:
            url += f"?token={ADMIN_TOKEN}"
        await update.message.reply_text(f"🔧 管理后台：\n{url}")

    async def _cmd_skilloff(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        from mochi.db import get_skill_mode, set_skill_mode
        if get_skill_mode() == "off":
            await update.message.reply_text("已经是闲聊模式啦~")
            return
        set_skill_mode("off")
        await update.message.reply_text("已切换到闲聊模式 ✦ 只保留记忆功能，省 token~")

    async def _cmd_skillon(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        from mochi.db import get_skill_mode, set_skill_mode
        if get_skill_mode() == "on":
            await update.message.reply_text("已经是完整模式啦~")
            return
        set_skill_mode("on")
        await update.message.reply_text("已恢复完整模式 ✦ 所有功能重新上线~")

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
        from mochi.heartbeat import should_wake_on_message, wake_up, clear_silent_pause
        if should_wake_on_message():
            wake_up("user_message")
        clear_silent_pause()

    async def _send_chat_result(self, chat_id: int, result) -> None:
        """Send a ChatResult — text message + any pending stickers."""
        if result.text:
            await self.send_message(chat_id, result.text)
        for file_id in result.stickers:
            await self.send_sticker(chat_id, file_id)
            import mochi.skills as skill_registry
            sticker_skill = skill_registry.get_skill("sticker")
            if sticker_skill:
                sticker_skill.record_last_sent(chat_id, file_id)

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return

        user_id = await self._check_owner(update)
        if user_id is None:
            return

        self._dispatch_state_signals()

        chat_id = update.effective_chat.id
        user_msg_id = update.message.message_id
        status = _StatusState()

        async def _on_interim(text=None, *, tool_name: str | None = None) -> None:
            # Refresh typing indicator
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass

            # Reaction: set 👨‍💻 on first tool call
            if TG_STATUS_REACTIONS_ENABLED and tool_name is not None:
                if status.reaction_state != "working":
                    status.reaction_state = "working"
                    await _set_reaction(context.bot, chat_id, user_msg_id, "\U0001F468\u200D\U0001F4BB")

            # Status message
            if not TG_STATUS_MESSAGE_ENABLED or tool_name is None:
                return

            label = _tool_label(tool_name)
            now = time.monotonic()
            same_label = label == status.last_label
            throttle_ok = (now - status.last_edit_time) >= TG_STATUS_EDIT_INTERVAL_S

            if same_label and status.status_msg_id is not None:
                return

            try:
                if status.status_msg_id is None:
                    sent = await update.message.reply_text(label)
                    status.status_msg_id = sent.message_id
                    status.last_edit_time = now
                    status.last_label = label
                elif throttle_ok:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status.status_msg_id,
                        text=label,
                    )
                    status.last_edit_time = now
                    status.last_label = label
            except Exception as e:
                if "not modified" not in str(e).lower():
                    log.debug("Status message update failed (ignored): %s", e)

        msg = IncomingMessage(
            user_id=user_id,
            channel_id=chat_id,
            text=update.message.text,
            transport="telegram",
            on_interim=_on_interim,
        )

        if _on_message_callback:
            from mochi.heartbeat import check_sleep_entry, handle_sleep_keyword
            if check_sleep_entry(update.message.text):
                await handle_sleep_keyword(user_id, update.message.text)
            else:
                result = None
                try:
                    result = await _on_message_callback(msg)
                finally:
                    # Finalize status UX: set 👍
                    if TG_STATUS_REACTIONS_ENABLED and status.reaction_state not in ("", "done"):
                        status.reaction_state = "done"
                        await _set_reaction(context.bot, chat_id, user_msg_id, "\U0001F44D")
                    # Clean up orphan status message on error
                    if result is None and status.status_msg_id:
                        try:
                            await context.bot.delete_message(
                                chat_id=chat_id, message_id=status.status_msg_id,
                            )
                        except Exception:
                            pass

                if result:
                    if status.status_msg_id and result.text:
                        # Edit status message into final reply, with bubble splitting
                        try:
                            bubbles = _split_bubbles(
                                result.text, TG_BUBBLE_MAX,
                                TG_BUBBLE_DELIMITER, TG_BUBBLE_MIN_CHARS,
                            )
                            # First bubble → edit status message in-place
                            first = bubbles[0]
                            for start in range(0, len(first), 4096):
                                if start == 0:
                                    await context.bot.edit_message_text(
                                        chat_id=chat_id,
                                        message_id=status.status_msg_id,
                                        text=first[:4096],
                                    )
                                else:
                                    await context.bot.send_message(
                                        chat_id=chat_id,
                                        text=first[start:start + 4096],
                                    )
                            # Remaining bubbles → send with typing delay
                            for bubble in bubbles[1:]:
                                await context.bot.send_chat_action(
                                    chat_id=chat_id, action="typing",
                                )
                                await asyncio.sleep(TG_BUBBLE_DELAY_S)
                                for start in range(0, len(bubble), 4096):
                                    await context.bot.send_message(
                                        chat_id=chat_id,
                                        text=bubble[start:start + 4096],
                                    )
                            # Stickers
                            for file_id in result.stickers:
                                await self.send_sticker(chat_id, file_id)
                                import mochi.skills as skill_registry
                                sticker_skill = skill_registry.get_skill("sticker")
                                if sticker_skill:
                                    sticker_skill.record_last_sent(chat_id, file_id)
                        except Exception:
                            # Fallback: send normally if edit fails
                            await self._send_chat_result(chat_id, result)
                    else:
                        await self._send_chat_result(chat_id, result)
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
