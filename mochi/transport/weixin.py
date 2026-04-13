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
        # WeChat user ID of the owner (learned from first inbound message)
        self._owner_weixin_id: str | None = None
        # context_token cache: weixin_user_id → latest context_token
        self._context_tokens: dict[str, str] = {}
        # typing ticket cache
        self._typing_tickets: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "wechat"

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
        self._poll_task = asyncio.create_task(self._poll_loop())
        log.info("WeChat transport started (base=%s)", WEIXIN_BASE_URL)

    async def stop(self) -> None:
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
            get_state, wake_up, clear_morning_hold, clear_silent_pause,
        )
        if get_state() == "SLEEPING":
            wake_up("user_message")
        clear_morning_hold()
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

            # Check sleep keywords after reply
            from mochi.heartbeat import check_sleep_entry
            if check_sleep_entry(text):
                pass  # Heartbeat handles the state transition
        else:
            # Cancel typing even if no callback
            if typing_ticket:
                await self._weixin_send_typing(
                    from_user, typing_ticket, status=2)

    # ── Long-poll loop ───────────────────────────────────────────────────

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
                            "WeChat session expired (errcode %s). "
                            "Re-login required: python weixin_auth.py",
                            errcode,
                        )
                        return

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
