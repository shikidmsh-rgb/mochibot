"""Telegram transport — sends and receives messages via Telegram Bot API.

This is the default transport. Requires TELEGRAM_BOT_TOKEN in .env.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from telegram import Update, ReactionTypeEmoji
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

from mochi.transport import (
    DeliveryError, Transport, IncomingMessage, ImageAttachment, ensure_delivery_allowed,
    MAX_IMAGE_BYTES,
)
from mochi.transport.utils import split_bubbles as _split_bubbles_util
from mochi.runtime_trace import delivery_method
from mochi.config import (
    TELEGRAM_BOT_TOKEN, set_owner_user_id,
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
    "update_core": "正在更新 Core…",
    "list_memories": "正在查记忆…",
    "manage_todo": "正在整理待办…",
    "habit_progress": "正在查看或记录习惯进度…",
    "edit_habit": "正在编辑习惯…",
    "log_meal": "正在记录饮食…",
    "query_meals": "正在查饮食…",
    "manage_reminder": "正在设置提醒…",
    "schedule_self_reminder": "正在安排稍后回望…",
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
        self._poll_task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return "telegram"

    async def start(self) -> None:
        if not TELEGRAM_BOT_TOKEN:
            log.warning("TELEGRAM_BOT_TOKEN not set, Telegram transport disabled")
            return

        self._app = Application.builder().token(TELEGRAM_BOT_TOKEN).updater(None).build()

        # Register handlers
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("heartbeat", self._cmd_heartbeat))
        self._app.add_handler(CommandHandler("cost", self._cmd_cost))
        self._app.add_handler(CommandHandler("core", self._cmd_core))
        self._app.add_handler(CommandHandler("diary", self._cmd_diary))
        self._app.add_handler(CommandHandler("restart", self._cmd_restart))
        self._app.add_handler(CommandHandler("skilloff", self._cmd_skilloff))
        self._app.add_handler(CommandHandler("skillon", self._cmd_skillon))
        self._app.add_handler(CommandHandler("reset", self._cmd_reset))
        self._app.add_handler(MessageHandler(
            (filters.TEXT & ~filters.COMMAND) | filters.PHOTO,
            self._handle_message,
        ))
        self._app.add_handler(
            MessageHandler(filters.Sticker.ALL, self._handle_sticker)
        )

        await self._app.initialize()
        await self._app.start()
        await self._app.bot.delete_webhook(drop_pending_updates=False)
        from mochi import conversation
        conversation.register("telegram", conversation.Adapter(
            self._respond_owner, self._owner_media,
        ))
        self._poll_task = asyncio.create_task(self._poll_loop())
        log.info("Telegram transport started")

    async def _poll_loop(self) -> None:
        offset = None
        while True:
            try:
                updates = await self._app.bot.get_updates(offset=offset, timeout=30)
                for update in updates:
                    message = update.message
                    command = bool(message and message.entities and any(
                        entity.type == "bot_command" and entity.offset == 0
                        for entity in message.entities
                    ))
                    context = self._app.context_types.context.from_update(update, self._app)
                    if message and message.sticker:
                        await self._handle_sticker(update, context)
                    elif message and (message.photo or message.text) and not command:
                        await self._handle_message(update, context)
                    else:
                        await self._app.process_update(update)
                    # The next poll acknowledges only input already admitted durably.
                    offset = update.update_id + 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Telegram intake failed; keeping the unacknowledged offset")
                await asyncio.sleep(2)

    async def stop(self) -> None:
        if self._app:
            if self._poll_task:
                self._poll_task.cancel()
                await asyncio.gather(self._poll_task, return_exceptions=True)
                self._poll_task = None
            await self._app.stop()
            await self._app.shutdown()
            log.info("Telegram transport stopped")

    async def send_message(
        self, user_id: int, text: str, *, can_deliver: Callable[[], bool] | None = None,
    ) -> bool:
        if not self._app:
            log.warning("Telegram not started, cannot send message")
            return False
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
                    ensure_delivery_allowed(can_deliver)
                    from mochi.conversation import deliver
                    chunk = bubble[start:start + 4096]
                    await deliver("text", chunk, lambda: self._app.bot.send_message(
                        chat_id=user_id, text=chunk,
                    ), rejected_errors=(BadRequest, Forbidden))
            return True
        except DeliveryError:
            raise
        except Exception as e:
            log.error("Failed to send Telegram message: %s", e)
            return False

    async def send_sticker(
        self, chat_id: int, file_id: str, *, can_deliver: Callable[[], bool] | None = None,
    ) -> bool:
        """Send a Telegram sticker by file_id."""
        if not self._app:
            return False
        try:
            ensure_delivery_allowed(can_deliver)
            from mochi.conversation import deliver
            await deliver("sticker", "", lambda: self._app.bot.send_sticker(
                chat_id=chat_id, sticker=file_id,
            ), rejected_errors=(BadRequest, Forbidden))
            return True
        except DeliveryError:
            raise
        except Exception as e:
            log.error("Failed to send sticker: %s", e)
            return False

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "我是你的 AI 伙伴，会记住我们的对话，在需要时提醒你。\n\n"
            "直接跟我聊天就行，也可以发一张图片给我看。\n\n"
            "指令：\n"
            "/help — 显示本帮助\n"
            "/heartbeat — 心跳状态\n"
            "/cost — Token 用量统计\n"
            "/core — 查看 Core\n"
            "/diary — 查看今日日記\n"
            "/skilloff — 闲聊模式（省 token）\n"
            "/skillon — 恢复完整模式\n"
            "/reset — 重置对话上下文（不影响长期记忆）\n"
            "/restart — 重启 Bot"
        )

    async def _cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        from mochi.conversation import invalidate
        invalidate(update.effective_user.id)
        await update.message.reply_text("正在重启...")
        from mochi.shutdown import request_restart
        request_restart(update.effective_chat.id)

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

    async def _cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        from mochi.db import set_context_reset
        set_context_reset(update.effective_user.id)
        from mochi.conversation import invalidate
        invalidate(update.effective_user.id)
        await update.message.reply_text("已重置对话上下文，bot 不会记得之前聊了什么。")

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
        from mochi.transport.utils import format_usage_summary
        await update.message.reply_text(format_usage_summary(get_usage_summary()))

    async def _cmd_core(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_owner(update.effective_user.id):
            return
        if update.effective_chat.type != "private":
            await update.message.reply_text("Core 只支持在私聊中查看。")
            return
        from mochi.core_store import read_core

        content = read_core()
        text = f"Core\n\n{content}" if content else "Core 为空。"
        for start in range(0, len(text), 4096):
            await update.message.reply_text(text[start:start + 4096])

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
        if update.effective_user is None:
            log.warning("Telegram: rejected an input without an identified sender")
            return None
        user_id = update.effective_user.id
        from mochi.config import OWNER_USER_ID as _current_owner
        if not _current_owner:
            set_owner_user_id(user_id)
            log.info("Owner auto-detected: user_id=%d", user_id)
        elif user_id != _current_owner:
            try:
                await update.message.reply_text("Sorry, I'm a personal companion bot.")
            except TelegramError as exc:
                log.warning("Telegram owner-only notice failed (%s)", type(exc).__name__)
            return None
        return user_id

    @staticmethod
    def _dispatch_state_signals() -> None:
        """Dispatch heartbeat state transitions on user activity."""
        from mochi.heartbeat import should_wake_on_message, wake_up, clear_silent_pause
        if should_wake_on_message():
            wake_up("user_message")
        clear_silent_pause()

    @delivery_method
    async def send_chat_result(
        self, chat_id: int, result, *, can_deliver: Callable[[], bool] | None = None,
    ) -> bool:
        """Send a ChatResult — text message + any pending stickers."""
        attempted = False
        delivered = True
        if result.text:
            attempted = True
            delivered = await self.send_message(
                chat_id, result.text, can_deliver=can_deliver,
            )
            if delivered:
                result.confirm_delivered()
        for file_id in result.stickers:
            attempted = True
            sticker_delivered = await self.send_sticker(
                chat_id, file_id, can_deliver=can_deliver,
            )
            delivered = sticker_delivered and delivered
            import mochi.skills as skill_registry
            sticker_skill = skill_registry.get_skill("sticker")
            if sticker_delivered and sticker_skill:
                sticker_skill.record_last_sent(chat_id, file_id)
        if not result.text and attempted and delivered:
            result.confirm_delivered()
        return attempted and delivered

    async def send_chat_result_checked(
        self, user_id: int, result, *, can_deliver: Callable[[], bool] | None = None,
    ) -> bool:
        if not await self.send_chat_result(user_id, result, can_deliver=can_deliver):
            raise DeliveryError(
                "telegram: transport did not confirm delivery",
                outcome="delivery_unknown",
            )
        return True

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not (update.message.text or update.message.photo):
            return

        user_id = await self._check_owner(update)
        if user_id is None:
            return

        self._queue_owner(update, user_id)

    def _queue_owner(self, update: Update, user_id: int) -> None:
        from mochi import conversation
        self._dispatch_state_signals()
        original = json.loads(update.to_json())
        payload = {
            "update_id": original["update_id"],
            "message": {
                key: value for key, value in original["message"].items()
                if key in {"message_id", "date", "from", "chat", "text", "caption", "photo", "sticker"}
            },
        }
        text = update.message.text or update.message.caption or ""
        if update.message.photo:
            text = f"[图片] {text or '请看看这张图片。'}"
        elif update.message.sticker:
            text = f"[用户发了一个贴纸 {update.message.sticker.emoji or ''}]" + (f" {text}" if text else "")
        conversation.receive(
            user_id=user_id, transport="telegram",
            source_id=f"telegram:{update.effective_chat.id}:{update.message.message_id}",
            text=text,
            payload={"update": payload},
        )

    async def _respond_owner(self, payload: dict) -> None:
        update = Update.de_json(payload["update"], self._app.bot)
        if not _is_owner(update.effective_user.id):
            raise PermissionError("Queued Telegram owner identity no longer matches")
        context = self._app.context_types.context.from_update(update, self._app)
        if update.message.sticker:
            await self._handle_owner_sticker(update, context, update.effective_user.id)
        else:
            await self._handle_owner_message(update, context, update.effective_user.id)

    async def _owner_media(self, payload: dict):
        update = Update.de_json(payload["update"], self._app.bot)
        if not _is_owner(update.effective_user.id):
            raise PermissionError("Prior Telegram input is not the current owner")
        if not update.message.photo:
            return None, None
        photo = update.message.photo[-1]
        if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
            raise ValueError("Telegram image exceeds the size limit")
        file = await self._app.bot.get_file(photo.file_id)
        image = ImageAttachment.from_bytes(bytes(await file.download_as_bytearray()))
        return image, None

    async def _edit_reply_start(self, chat_id: int, message_id: int, text: str) -> None:
        from mochi.conversation import deliver

        try:
            await deliver("text", text, lambda: self._app.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
            ), rejected_errors=(BadRequest, Forbidden))
        except BadRequest:
            # Only the explicitly rejected first packet is replaced, never the whole reply.
            await deliver("text", text, lambda: self._app.bot.send_message(
                chat_id=chat_id, text=text,
            ), rejected_errors=(BadRequest, Forbidden))

    async def _handle_owner_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int,
    ) -> None:
        from mochi.conversation import checkpoint, wait_read
        checkpoint()
        image = None
        text = update.message.text or update.message.caption or "请看看这张图片。"
        if update.message.photo:
            photo = update.message.photo[-1]  # Telegram orders photos by size
            if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
                await update.message.reply_text("图片太大了，请发送 5 MB 以内的图片。")
                return
            try:
                tg_file = await wait_read(context.bot.get_file(photo.file_id))
                data = bytes(await wait_read(tg_file.download_as_bytearray()))
            except Exception as e:
                log.warning("Failed to download Telegram image: %s", e)
                await update.message.reply_text("图片下载失败了，请重新发送一次。")
                return
            if len(data) > MAX_IMAGE_BYTES:
                await update.message.reply_text("图片太大了，请发送 5 MB 以内的图片。")
                return
            image = ImageAttachment(data=data, media_type="image/jpeg")

        checkpoint()
        self._dispatch_state_signals()

        chat_id = update.effective_chat.id
        user_msg_id = update.message.message_id
        status = _StatusState()

        async def _on_interim(text=None, *, tool_name: str | None = None) -> None:
            from mochi.conversation import checkpoint
            checkpoint()
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
            text=text,
            transport="telegram",
            owner_authorized=True,
            image=image,
            on_interim=_on_interim,
        )

        if _on_message_callback:
            from mochi.heartbeat import (
                claim_sleep_transition,
                go_to_sleep,
            )
            bedtime_claimed = False

            async def _process_main_turn() -> None:
                nonlocal bedtime_claimed
                from mochi.conversation import checkpoint, deliver
                checkpoint()
                result = None
                try:
                    result = await _on_message_callback(msg)
                finally:
                    # Finalize status UX: set 👍
                    if TG_STATUS_REACTIONS_ENABLED and status.reaction_state not in ("", "done"):
                        from mochi.conversation import is_current
                        completed = result is not None and is_current()
                        status.reaction_state = "done" if completed else ""
                        await _set_reaction(
                            context.bot, chat_id, user_msg_id, "\U0001F44D" if completed else None,
                        )
                    # Clean up orphan status message on error
                    if result is None and status.status_msg_id:
                        try:
                            await context.bot.delete_message(
                                chat_id=chat_id, message_id=status.status_msg_id,
                            )
                        except Exception:
                            pass

                if result:
                    if result.bedtime_requested:
                        checkpoint()
                        bedtime_claimed = claim_sleep_transition("explicit")
                        if (
                            not bedtime_claimed
                            or result.disposition in {"skip", "handled"}
                        ):
                            if not bedtime_claimed and result.disposition == "deliver":
                                from mochi.runtime_trace import record_delivery
                                record_delivery(result, "suppressed")
                            if TG_STATUS_REACTIONS_ENABLED:
                                status.reaction_state = ""
                                await _set_reaction(
                                    context.bot, chat_id, user_msg_id, None,
                                )
                            if status.status_msg_id:
                                try:
                                    await context.bot.delete_message(
                                        chat_id=chat_id,
                                        message_id=status.status_msg_id,
                                    )
                                except Exception:
                                    pass
                            return
                    delivered = False
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
                                    await self._edit_reply_start(chat_id, status.status_msg_id, first[:4096])
                                else:
                                    await deliver("text", first[start:start + 4096], lambda: context.bot.send_message(
                                        chat_id=chat_id,
                                        text=first[start:start + 4096],
                                    ), rejected_errors=(BadRequest, Forbidden))
                            # Remaining bubbles → send with typing delay
                            for bubble in bubbles[1:]:
                                await context.bot.send_chat_action(
                                    chat_id=chat_id, action="typing",
                                )
                                await asyncio.sleep(TG_BUBBLE_DELAY_S)
                                for start in range(0, len(bubble), 4096):
                                    await deliver("text", bubble[start:start + 4096], lambda: context.bot.send_message(
                                        chat_id=chat_id,
                                        text=bubble[start:start + 4096],
                                    ), rejected_errors=(BadRequest, Forbidden))
                            # Stickers
                            delivered = True
                            result.confirm_delivered()
                            for file_id in result.stickers:
                                sticker_delivered = await self.send_sticker(
                                    chat_id, file_id,
                                )
                                delivered = sticker_delivered and delivered
                                import mochi.skills as skill_registry
                                sticker_skill = skill_registry.get_skill("sticker")
                                if sticker_delivered and sticker_skill:
                                    sticker_skill.record_last_sent(chat_id, file_id)
                        except Exception:
                            # Earlier packets may already be visible; never replay the whole reply.
                            raise
                    else:
                        delivered = await self.send_chat_result(chat_id, result)
                    if delivered:
                        result.confirm_delivered(final=True)
                    else:
                        from mochi.runtime_trace import record_delivery
                        record_delivery(result, "delivery_unknown")

            try:
                await _process_main_turn()
            except Exception as exc:
                if not bedtime_claimed:
                    raise
                log.error("Telegram Bedtime entry failed: %s", exc, exc_info=True)
            finally:
                if bedtime_claimed:
                    from mochi.conversation import is_current
                    if is_current():
                        go_to_sleep("explicit")
                    else:
                        from mochi.heartbeat import cancel_sleep_transition
                        cancel_sleep_transition()
        else:
            await update.message.reply_text("I'm still waking up... try again in a moment.")

    async def _handle_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.sticker:
            return

        user_id = await self._check_owner(update)
        if user_id is None:
            return

        self._queue_owner(update, user_id)

    async def _handle_owner_sticker(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int,
    ) -> None:
        self._dispatch_state_signals()

        sticker = update.message.sticker
        msg = IncomingMessage(
            user_id=user_id,
            channel_id=update.effective_chat.id,
            text=update.message.caption or "",
            transport="telegram",
            owner_authorized=True,
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
                delivered = await self.send_chat_result(
                    update.effective_chat.id, result,
                )
                if delivered:
                    result.confirm_delivered(final=True)
        else:
            await update.message.reply_text("I'm still waking up... try again in a moment.")
