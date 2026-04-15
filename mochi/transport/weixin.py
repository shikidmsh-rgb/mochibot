"""WeChat transport — sends and receives messages via WeChat iLink Bot API.

Optional secondary transport. Requires WEIXIN_ENABLED=true and
WEIXIN_BOT_TOKEN in .env. Run `python weixin_auth.py` to obtain a token.
"""

import asyncio
import base64
import json
import logging
import os
import struct
from typing import Any

from mochi.transport import Transport, IncomingMessage
from mochi.transport.utils import clean_reply_markers, split_bubbles, split_text
from mochi.config import (
    OWNER_USER_ID,
    WEIXIN_ALLOWED_USERS,
    WEIXIN_BACKOFF_MAX_S,
    WEIXIN_BACKOFF_MIN_S,
    WEIXIN_BASE_URL,
    WEIXIN_BOT_TOKEN,
    WEIXIN_BUBBLE_DELAY_S,
    WEIXIN_MAX_CONSECUTIVE_FAILURES,
    WEIXIN_MSG_LIMIT,
    WEIXIN_POLL_TIMEOUT_S,
    WEIXIN_SESSION_EXPIRED_RETRY_S,
)

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

SESSION_EXPIRED_ERRCODE = -14

# WeChat item types (from item_list[].type)
_ITEM_TEXT = 1
_ITEM_IMAGE = 2
_ITEM_VOICE = 3

# WeChat message_type field
_MSG_TYPE_USER = 1
_MSG_TYPE_BOT = 2

# ── Module-level callback (same pattern as telegram.py) ──────────────────────

_on_message_callback = None


def set_message_handler(callback) -> None:
    """Register the function to handle incoming messages.

    Signature: async def callback(msg: IncomingMessage) -> ChatResult
    """
    global _on_message_callback
    _on_message_callback = callback


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _random_wechat_uin() -> str:
    """Generate X-WECHAT-UIN header: random uint32 -> decimal -> base64."""
    uint32 = struct.unpack(">I", os.urandom(4))[0]
    return base64.b64encode(str(uint32).encode()).decode()


def _build_headers() -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    if WEIXIN_BOT_TOKEN:
        headers["Authorization"] = f"Bearer {WEIXIN_BOT_TOKEN}"
    return headers


# ── Message text extraction ──────────────────────────────────────────────────

def _extract_text(item_list: list[dict]) -> str:
    """Extract text content from item_list. Returns empty string if none."""
    for item in item_list:
        if item.get("type") == _ITEM_TEXT:
            text_item = item.get("text_item", {})
            text = text_item.get("text", "")
            if text:
                ref = item.get("ref_msg")
                if ref:
                    title = ref.get("title", "")
                    if title:
                        return f"[引用「{title}」]\n{text}"
                return text
        if item.get("type") == _ITEM_VOICE:
            voice_item = item.get("voice_item", {})
            voice_text = voice_item.get("text", "")
            if voice_text:
                return voice_text
    return ""


# ── Security ─────────────────────────────────────────────────────────────────

def _is_allowed(from_user: str) -> bool:
    """Check if the sender is in the allowlist. Empty list = allow all."""
    if not WEIXIN_ALLOWED_USERS:
        return True
    return from_user in WEIXIN_ALLOWED_USERS


# ── Transport class ──────────────────────────────────────────────────────────

class WeixinTransport(Transport):
    """WeChat iLink Bot API transport."""

    def __init__(self):
        self._session = None  # aiohttp.ClientSession
        self._poll_task: asyncio.Task | None = None
        self._stopped = False
        self._session_expired = False
        # WeChat user ID of the owner (learned from first inbound message)
        self._owner_weixin_id: str | None = None
        # context_token cache: weixin_user_id → latest context_token
        self._context_tokens: dict[str, str] = {}
        # typing ticket cache
        self._typing_tickets: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "wechat"

    @property
    def session_expired(self) -> bool:
        """True when iLink session is expired and awaiting QR re-scan."""
        return self._session_expired

    def restore_owner_id(self, weixin_id: str) -> None:
        """Pre-set the owner WeChat ID (used after restart)."""
        self._owner_weixin_id = weixin_id
        log.info("WeChat: owner ID restored from restart flag: %s", weixin_id)

    async def start(self) -> None:
        try:
            import aiohttp
        except ImportError:
            log.error(
                "aiohttp is required for WeChat transport. "
                "Install it: pip install aiohttp"
            )
            return

        if not WEIXIN_BOT_TOKEN:
            log.warning("WEIXIN_BOT_TOKEN not set, WeChat transport disabled")
            return

        import aiohttp
        self._session = aiohttp.ClientSession()
        self._stopped = False
        self._session_expired = False
        self._poll_task = asyncio.create_task(self._supervised_poll_loop())
        log.info("WeChat transport started (base=%s)", WEIXIN_BASE_URL)

    async def stop(self) -> None:
        self._stopped = True
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._session:
            await self._session.close()
            self._session = None
        log.info("WeChat transport stopped")

    async def send_message(self, user_id: int, text: str) -> None:
        """Send text to the owner's WeChat.

        user_id is the internal OWNER_USER_ID (int). Mapped internally
        to the WeChat string ID for API delivery.
        """
        if not self._session or not self._owner_weixin_id:
            log.warning("WeChat: cannot send — session or owner ID not ready")
            return

        # Clean any side-channel markers
        text = clean_reply_markers(text)
        if not text:
            return

        weixin_id = self._owner_weixin_id
        context_token = self._context_tokens.get(weixin_id, "")

        bubbles = split_bubbles(text)
        for i, bubble in enumerate(bubbles):
            if i > 0:
                await asyncio.sleep(WEIXIN_BUBBLE_DELAY_S)
            for chunk in split_text(bubble, WEIXIN_MSG_LIMIT):
                try:
                    await self._weixin_send_message(weixin_id, chunk, context_token)
                except Exception as e:
                    log.error("WeChat: send error to %s: %s", weixin_id, e)

    # ── HTTP API layer ───────────────────────────────────────────────────

    async def _api_post(self, endpoint: str, body: dict[str, Any],
                        timeout_s: int = 15) -> dict:
        """POST JSON to a WeChat API endpoint."""
        assert self._session is not None
        import aiohttp
        url = f"{WEIXIN_BASE_URL.rstrip('/')}/{endpoint}"
        headers = _build_headers()
        try:
            async with self._session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                raw = await resp.text()
                log.debug("WeChat API %s HTTP %s (len=%d)",
                          endpoint, resp.status, len(raw))
                if not resp.ok:
                    log.error("WeChat API %s HTTP %s: %s",
                              endpoint, resp.status, raw[:200])
                    return {"ret": -1, "errmsg": f"HTTP {resp.status}"}
                return json.loads(raw)
        except asyncio.TimeoutError:
            log.info("WeChat API %s timeout after %ss (normal for long-poll)",
                     endpoint, timeout_s)
            return {"ret": 0, "msgs": []}
        except Exception as e:
            log.error("WeChat API %s error: %s", endpoint, e)
            raise

    async def _weixin_get_updates(self, get_updates_buf: str,
                                  timeout_s: int) -> dict:
        return await self._api_post("ilink/bot/getupdates", {
            "get_updates_buf": get_updates_buf,
        }, timeout_s=timeout_s)

    async def _weixin_send_message(self, to: str, text: str,
                                   context_token: str) -> dict:
        client_id = f"mochi-weixin-{struct.unpack('>I', os.urandom(4))[0]}"
        return await self._api_post("ilink/bot/sendmessage", {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": client_id,
                "message_type": _MSG_TYPE_BOT,
                "message_state": 2,  # FINISH
                "context_token": context_token,
                "item_list": [
                    {"type": _ITEM_TEXT, "text_item": {"text": text}},
                ],
            },
            "base_info": {"channel_version": "1.0.0"},
        })

    async def _weixin_get_config(self, user_id: str,
                                 context_token: str = "") -> dict:
        return await self._api_post("ilink/bot/getconfig", {
            "ilink_user_id": user_id,
            "context_token": context_token,
        }, timeout_s=10)

    async def _weixin_send_typing(self, user_id: str, ticket: str,
                                  status: int = 1) -> None:
        """Send typing indicator. status: 1=typing, 2=cancel."""
        try:
            await self._api_post("ilink/bot/sendtyping", {
                "ilink_user_id": user_id,
                "typing_ticket": ticket,
                "status": status,
            }, timeout_s=10)
        except Exception as e:
            log.debug("WeChat typing error: %s", e)

    # ── Typing ticket cache ──────────────────────────────────────────────

    async def _get_typing_ticket(self, user_id: str,
                                 context_token: str) -> str | None:
        cached = self._typing_tickets.get(user_id)
        if cached:
            return cached
        try:
            resp = await self._weixin_get_config(user_id, context_token)
            ticket = resp.get("typing_ticket", "")
            if ticket:
                self._typing_tickets[user_id] = ticket
                return ticket
        except Exception as e:
            log.debug("Failed to get typing ticket for %s: %s", user_id, e)
        return None

    # ── Heartbeat state signals ──────────────────────────────────────────

    @staticmethod
    def _dispatch_state_signals() -> None:
        """Dispatch heartbeat state transitions on user activity."""
        from mochi.heartbeat import (
            should_wake_on_message, wake_up, clear_silent_pause,
        )
        if should_wake_on_message():
            wake_up("user_message")
        clear_silent_pause()

    # ── Message handling ─────────────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        """Process one inbound WeChat message."""
        from_user = msg.get("from_user_id", "")
        if not from_user:
            return

        if not _is_allowed(from_user):
            log.info("WeChat: rejected message from unlisted user %s",
                     from_user)
            return

        text = _extract_text(msg.get("item_list", []))
        if not text:
            log.info("WeChat: non-text message from %s, skipping", from_user)
            return

        # Cache context_token for replies
        context_token = msg.get("context_token", "")
        if context_token:
            self._context_tokens[from_user] = context_token

        # Learn the owner's WeChat ID from the first allowed message
        if self._owner_weixin_id is None:
            self._owner_weixin_id = from_user
            log.info("WeChat: owner ID learned: %s", from_user)

        # System command: /restart (owner only)
        if text.strip() == "/restart":
            if from_user != self._owner_weixin_id:
                return
            try:
                await self._weixin_send_message(
                    from_user, "正在重启...", context_token)
            except Exception as e:
                log.warning("WeChat: failed to send restart ack: %s", e)
            from mochi.shutdown import request_restart
            request_restart(OWNER_USER_ID or 0, weixin_id=from_user)
            return

        # System command: /help
        if text.strip() == "/help":
            help_text = (
                "我是你的 AI 伙伴，会记住我们的对话，在需要时提醒你。\n\n"
                "直接跟我聊天就行，不用特殊格式。\n\n"
                "指令：\n"
                "/help — 显示本帮助\n"
                "/heartbeat — 心跳状态\n"
                "/cost — Token 用量统计\n"
                "/notes — 查看笔记\n"
                "/diary — 查看今日日記\n"
                "/admin — 管理后台\n"
                "/restart — 重启 Bot"
            )
            try:
                await self._weixin_send_message(
                    from_user, help_text, context_token)
            except Exception as e:
                log.warning("WeChat: failed to send help: %s", e)
            return

        # System command: /admin (owner only)
        if text.strip() == "/admin":
            if from_user != self._owner_weixin_id:
                return
            from mochi.config import ADMIN_PORT, ADMIN_BIND, ADMIN_TOKEN, _detect_host_ip
            # /admin is always sent from a remote device (phone), so use LAN IP
            host = _detect_host_ip() or ADMIN_BIND
            if host in ("0.0.0.0", "127.0.0.1", "localhost", "::1"):
                host = "<your-server-ip>"
            url = f"http://{host}:{ADMIN_PORT}"
            if ADMIN_TOKEN:
                url += f"?token={ADMIN_TOKEN}"
            try:
                await self._weixin_send_message(from_user, f"🔧 管理后台：\n{url}", context_token)
            except Exception as e:
                log.warning("WeChat: failed to send admin URL: %s", e)
            return

        # System command: /heartbeat (owner only)
        if text.strip() == "/heartbeat":
            if from_user != self._owner_weixin_id:
                return
            from mochi.heartbeat import get_stats
            from mochi.db import get_last_heartbeat_log
            stats = get_stats()
            entry = get_last_heartbeat_log()
            lines = [
                "📊 心跳状态",
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
            try:
                await self._weixin_send_message(
                    from_user, "\n".join(lines), context_token)
            except Exception as e:
                log.warning("WeChat: failed to send heartbeat: %s", e)
            return

        # System command: /cost (owner only)
        if text.strip() == "/cost":
            if from_user != self._owner_weixin_id:
                return
            from mochi.db import get_usage_summary
            s = get_usage_summary()

            def _format_block(title: str, by_model: dict) -> list[str]:
                block = [title]
                if not by_model:
                    block.append("  (无记录)")
                    return block
                for model, data in sorted(by_model.items()):
                    block.append(f"  {model}")
                    block.append(f"    input {data['prompt']:,}  |  output {data['completion']:,}")
                return block

            lines = _format_block("📊 今日", s["today"]["by_model"])
            lines.append("")
            lines += _format_block("📊 本月", s["month"]["by_model"])
            try:
                await self._weixin_send_message(
                    from_user, "\n".join(lines), context_token)
            except Exception as e:
                log.warning("WeChat: failed to send cost: %s", e)
            return

        # System command: /notes (owner only)
        if text.strip() == "/notes":
            if from_user != self._owner_weixin_id:
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
                reply = "No notes."
            else:
                reply = "📝 Notes\n" + "\n".join(
                    f"{i+1}. {n}" for i, n in enumerate(notes))
            try:
                await self._weixin_send_message(
                    from_user, reply, context_token)
            except Exception as e:
                log.warning("WeChat: failed to send notes: %s", e)
            return

        # System command: /diary (owner only)
        if text.strip() == "/diary":
            if from_user != self._owner_weixin_id:
                return
            from mochi.diary import diary
            from mochi.config import logical_today
            status = diary.read(section="今日状態") or "(无)"
            journal = diary.read(section="今日日記") or "(无)"
            today = logical_today()
            reply = (
                f"📖 今日日記 ({today})\n\n"
                f"── 今日状態 ──\n{status}\n\n"
                f"── 今日日記 ──\n{journal}"
            )
            try:
                await self._weixin_send_message(
                    from_user, reply, context_token)
            except Exception as e:
                log.warning("WeChat: failed to send diary: %s", e)
            return

        # Heartbeat wake signals
        try:
            self._dispatch_state_signals()
        except Exception as e:
            log.debug("WeChat: heartbeat signal error (non-fatal): %s", e)

        # Show typing
        typing_ticket = await self._get_typing_ticket(from_user, context_token)
        if typing_ticket:
            await self._weixin_send_typing(from_user, typing_ticket, status=1)

        # Build IncomingMessage with int user_id (owner mapping)
        user_id = OWNER_USER_ID or 0
        incoming = IncomingMessage(
            user_id=user_id,
            channel_id=user_id,
            text=text,
            transport="wechat",
            raw={"weixin_user_id": from_user},
        )

        # Call chat via callback
        if _on_message_callback:
            from mochi.heartbeat import check_sleep_entry, handle_sleep_keyword
            if check_sleep_entry(text):
                # Goodnight keyword → bedtime tidy handles the goodbye.
                # Skip normal Chat to avoid double goodnight message.
                if typing_ticket:
                    await self._weixin_send_typing(
                        from_user, typing_ticket, status=2)
                await handle_sleep_keyword(user_id, text)
            else:
                try:
                    result = await _on_message_callback(incoming)
                except Exception as e:
                    log.error("WeChat: chat error for %s: %s", from_user, e)
                    result = None

                # Cancel typing
                if typing_ticket:
                    await self._weixin_send_typing(
                        from_user, typing_ticket, status=2)

                if result and result.text:
                    reply = clean_reply_markers(result.text)
                    if reply:
                        bubbles = split_bubbles(reply)
                        for i, bubble in enumerate(bubbles):
                            if i > 0:
                                await asyncio.sleep(WEIXIN_BUBBLE_DELAY_S)
                            for chunk in split_text(bubble, WEIXIN_MSG_LIMIT):
                                try:
                                    await self._weixin_send_message(
                                        from_user, chunk, context_token)
                                except Exception as e:
                                    log.error("WeChat: send error: %s", e)
        else:
            # Cancel typing even if no callback
            if typing_ticket:
                await self._weixin_send_typing(
                    from_user, typing_ticket, status=2)

    # ── Long-poll loop ───────────────────────────────────────────────────

    async def _supervised_poll_loop(self) -> None:
        """Restart _poll_loop on session expiry with backoff.

        If _poll_loop exits due to session expiry (errcode -14), the
        supervisor retries periodically until the session recovers
        (user re-scans QR code) or the transport is stopped.
        """
        while True:
            self._session_expired = False
            await self._poll_loop()

            if self._stopped:
                break

            if not self._session_expired:
                break

            # Session expired — retry after delay
            log.warning(
                "Poll loop exited (session expired). "
                "Retrying in %ds... Re-login: python weixin_auth.py",
                WEIXIN_SESSION_EXPIRED_RETRY_S,
            )

            while self._session_expired and not self._stopped:
                await asyncio.sleep(WEIXIN_SESSION_EXPIRED_RETRY_S)
                if self._stopped:
                    break
                log.info("Probing WeChat session recovery...")
                try:
                    probe = await self._weixin_get_updates("", timeout_s=5)
                    probe_err = probe.get("errcode", 0)
                    probe_ret = probe.get("ret", 0)
                    if (probe_err == SESSION_EXPIRED_ERRCODE
                            or probe_ret == SESSION_EXPIRED_ERRCODE):
                        log.warning(
                            "Session still expired, will retry in %ds",
                            WEIXIN_SESSION_EXPIRED_RETRY_S,
                        )
                        continue
                    # Recovered!
                    log.info("[SESSION_RECOVERED] WeChat session recovered!")
                    self._session_expired = False
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning(
                        "Retry probe failed: %s — will retry in %ds",
                        e, WEIXIN_SESSION_EXPIRED_RETRY_S,
                    )
                    continue

    async def _poll_loop(self) -> None:
        """Main long-poll loop for WeChat messages."""
        get_updates_buf = ""
        consecutive_failures = 0
        _tasks: set[asyncio.Task] = set()

        log.info("WeChat poll loop started (timeout=%ss)",
                 WEIXIN_POLL_TIMEOUT_S)

        while True:
            try:
                resp = await self._weixin_get_updates(
                    get_updates_buf, WEIXIN_POLL_TIMEOUT_S)

                ret = resp.get("ret", 0)
                errcode = resp.get("errcode", 0)
                msgs = resp.get("msgs", [])

                if ret != 0 or errcode != 0:
                    if (errcode == SESSION_EXPIRED_ERRCODE
                            or ret == SESSION_EXPIRED_ERRCODE):
                        log.error(
                            "[SESSION_EXPIRED] WeChat session expired "
                            "(errcode %s). Re-login required: "
                            "python weixin_auth.py",
                            errcode,
                        )
                        self._session_expired = True
                        return  # supervisor will handle retry

                    consecutive_failures += 1
                    log.warning(
                        "WeChat getUpdates error: ret=%s errcode=%s (%d/%d)",
                        ret, errcode,
                        consecutive_failures, WEIXIN_MAX_CONSECUTIVE_FAILURES,
                    )
                    if consecutive_failures >= WEIXIN_MAX_CONSECUTIVE_FAILURES:
                        await asyncio.sleep(WEIXIN_BACKOFF_MAX_S)
                        consecutive_failures = 0
                    else:
                        await asyncio.sleep(WEIXIN_BACKOFF_MIN_S)
                    continue

                consecutive_failures = 0

                new_buf = resp.get("get_updates_buf", "")
                if new_buf:
                    get_updates_buf = new_buf

                for msg in msgs:
                    if msg.get("message_type") == _MSG_TYPE_USER:
                        task = asyncio.create_task(
                            self._handle_message(msg))
                        _tasks.add(task)
                        task.add_done_callback(_tasks.discard)

            except asyncio.CancelledError:
                log.info("WeChat poll loop cancelled")
                break
            except Exception as e:
                consecutive_failures += 1
                log.error("WeChat poll error (%d/%d): %s",
                          consecutive_failures,
                          WEIXIN_MAX_CONSECUTIVE_FAILURES, e)
                if consecutive_failures >= WEIXIN_MAX_CONSECUTIVE_FAILURES:
                    await asyncio.sleep(WEIXIN_BACKOFF_MAX_S)
                    consecutive_failures = 0
                else:
                    await asyncio.sleep(WEIXIN_BACKOFF_MIN_S)
