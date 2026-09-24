"""WeChat transport — sends and receives messages via WeChat iLink Bot API.

Optional secondary transport. Requires WEIXIN_ENABLED=true and
WEIXIN_BOT_TOKEN in .env. Run `python scripts/weixin_auth.py` to obtain a token.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from mochi.transport import (
    DeliveryError, Transport, IncomingMessage, ImageAttachment, MAX_IMAGE_BYTES,
    ensure_delivery_allowed,
)
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
_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

# WeChat item types (from item_list[].type)
_ITEM_TEXT = 1
_ITEM_IMAGE = 2
_ITEM_VOICE = 3

# WeChat message_type field
_MSG_TYPE_USER = 1
_MSG_TYPE_BOT = 2


class _ImageTooLargeError(ValueError):
    pass


def _validate_cdn_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not (parsed.hostname or "").endswith(".weixin.qq.com")
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Image URL is not a WeChat HTTPS CDN URL")


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

    def restore_owner_id(self, weixin_id: str, *, source: str = "restart flag") -> None:
        """Pre-set the owner WeChat ID (used after restart)."""
        self._owner_weixin_id = weixin_id
        log.info("WeChat: owner ID restored (%s): %s", source, weixin_id)
        from mochi.admin.admin_crypto import decrypt_api_key
        from mochi.db import get_skill_config

        raw = get_skill_config("_transport:wechat").get("reply_context")
        if not raw:
            return
        try:
            saved = json.loads(raw)
        except ValueError:
            log.warning("WeChat: stored reply context is invalid")
            return
        if not isinstance(saved, dict):
            log.warning("WeChat: stored reply context is not an object")
            return
        if (
            saved.get("account") != self._context_account()
            or saved.get("owner") != weixin_id
        ):
            log.info("WeChat: ignoring reply context for a different account or owner")
            return
        encrypted = saved.get("token")
        if not isinstance(encrypted, str):
            log.warning("WeChat: stored reply context has no valid token")
            return
        token = decrypt_api_key(encrypted)
        if token:
            self._context_tokens[weixin_id] = token
            log.info("WeChat: reply context restored")

    @staticmethod
    def _context_account() -> str:
        identity = f"{WEIXIN_BASE_URL.rstrip('/')}\n{WEIXIN_BOT_TOKEN}"
        return hashlib.sha256(identity.encode()).hexdigest()

    def _remember_context_token(self, weixin_id: str, token: str) -> None:
        if self._context_tokens.get(weixin_id) == token:
            return
        if weixin_id == self._owner_weixin_id:
            from mochi.admin.admin_crypto import encrypt_api_key
            from mochi.db import set_skill_config

            set_skill_config(
                "_transport:wechat",
                "reply_context",
                json.dumps({
                    "account": self._context_account(),
                    "owner": weixin_id,
                    "token": encrypt_api_key(token),
                }),
            )
        self._context_tokens[weixin_id] = token

    def _invalidate_context_token(self, weixin_id: str, token: str) -> None:
        # A late response for an older send must not erase a new inbound token.
        if self._context_tokens.get(weixin_id) == token:
            self._remember_context_token(weixin_id, "")
            log.warning("WeChat: reply context invalidated after session rejection")

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

    async def send_message(self, user_id: int, text: str) -> bool:
        """Send text to the owner's WeChat.

        user_id is the internal OWNER_USER_ID (int). Mapped internally
        to the WeChat string ID for API delivery.
        """
        if not self._session or not self._owner_weixin_id:
            log.warning("WeChat: cannot send — session or owner ID not ready")
            return False

        weixin_id = self._owner_weixin_id
        context_token = self._context_tokens.get(weixin_id, "")
        return await self._send_text(weixin_id, text, context_token)

    async def _send_text(
        self,
        weixin_id: str,
        text: str,
        context_token: str,
    ) -> bool:
        """Send all text bubbles and report complete delivery."""
        try:
            return await self._send_text_checked(weixin_id, text, context_token)
        except DeliveryError as exc:
            log.warning("WeChat: %s: %s", exc.outcome, exc)
            return False

    async def _send_text_checked(
        self, weixin_id: str, text: str, context_token: str,
        *, can_deliver: Callable[[], bool] | None = None,
        single_message: bool = False,
    ) -> bool:
        text = clean_reply_markers(text)
        if single_message:
            text = text.replace("|||", "\n\n").strip()
        if not text:
            raise DeliveryError("wechat: empty text", outcome="delivery_unavailable")
        bubbles = [text] if single_message else split_bubbles(text)
        for i, bubble in enumerate(bubbles):
            if i > 0:
                await asyncio.sleep(WEIXIN_BUBBLE_DELAY_S)
            for chunk in split_text(bubble, WEIXIN_MSG_LIMIT):
                ensure_delivery_allowed(can_deliver)
                await self._weixin_send_message(weixin_id, chunk, context_token)
        return True

    async def send_chat_result(
        self,
        user_id: int,
        result,
        *,
        context_token: str | None = None,
    ) -> bool:
        try:
            return await self.send_chat_result_checked(
                user_id, result, context_token=context_token,
            )
        except DeliveryError as exc:
            log.warning("WeChat: %s: %s", exc.outcome, exc)
            return False

    async def send_chat_result_checked(
        self, user_id: int, result, *, context_token: str | None = None,
        can_deliver: Callable[[], bool] | None = None,
        single_message: bool = False,
    ) -> bool:
        if not self._session or not self._owner_weixin_id or not result.text:
            raise DeliveryError(
                "wechat: session, owner or text not ready",
                outcome="delivery_unavailable",
            )
        token = (
            context_token
            if context_token is not None
            else self._context_tokens.get(self._owner_weixin_id, "")
        )
        delivered = await self._send_text_checked(
            self._owner_weixin_id, result.text, token, can_deliver=can_deliver,
            single_message=single_message,
        )
        if delivered:
            result.confirm_delivered()
        return delivered

    async def send_proactive_result_checked(
        self, user_id: int, result, *, can_deliver: Callable[[], bool] | None = None,
    ) -> bool:
        return await self.send_chat_result_checked(
            user_id, result, can_deliver=can_deliver, single_message=True,
        )

    async def send_image_checked(
        self, user_id: int, image: ImageAttachment,
        *, can_deliver: Callable[[], bool] | None = None,
    ) -> None:
        import aiohttp

        ensure_delivery_allowed(can_deliver)
        to = self._owner_weixin_id
        token = self._context_tokens.get(to, "") if to else ""
        if not to or not self._session or self._session_expired or not token:
            raise DeliveryError(
                "wechat: session, owner or reply context not ready",
                outcome="delivery_unavailable",
            )
        ImageAttachment.from_bytes(image.data)
        key = os.urandom(16)
        filekey = os.urandom(16).hex()
        padder = padding.PKCS7(128).padder()
        padded = padder.update(image.data) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        try:
            response = await self._api_post("ilink/bot/getuploadurl", {
                "filekey": filekey, "media_type": 1, "to_user_id": to,
                "rawsize": len(image.data),
                "rawfilemd5": hashlib.md5(image.data).hexdigest(),
                "filesize": len(ciphertext), "no_need_thumb": True,
                "aeskey": key.hex(), "base_info": {"channel_version": "1.0.0"},
            })
            if not isinstance(response, dict):
                raise ValueError("Invalid image upload authorization")
            if response.get("ret", 0) != 0 or response.get("errcode", 0) != 0:
                raise ValueError("Image upload authorization rejected")
            url = response.get("upload_full_url")
            if not url:
                param = response.get("upload_param")
                if not param:
                    raise ValueError("Image upload reference missing")
                query = urlencode({"encrypted_query_param": param, "filekey": filekey})
                url = f"{_CDN_BASE_URL}/upload?{query}"
            _validate_cdn_url(url)
            ensure_delivery_allowed(can_deliver)
            async with self._session.post(
                url, data=ciphertext, headers={"Content-Type": "application/octet-stream"},
                timeout=aiohttp.ClientTimeout(total=60), allow_redirects=False,
            ) as uploaded:
                if uploaded.status != 200:
                    raise ValueError(f"Image upload HTTP {uploaded.status}")
                download_param = uploaded.headers.get("x-encrypted-param")
                if not download_param:
                    raise ValueError("Image upload receipt missing")
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            # Uploading bytes alone never delivers a chat message.
            log.warning("WeChat: image upload failed (%s)", type(exc).__name__)
            raise DeliveryError(
                "wechat: image upload failed; no image message sent",
                outcome="delivery_unavailable",
            ) from exc
        ensure_delivery_allowed(can_deliver)
        await self._weixin_send_item(to, {
            "type": _ITEM_IMAGE,
            "image_item": {
                "media": {
                    "encrypt_query_param": download_param,
                    "aes_key": base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
                    "encrypt_type": 1,
                },
                "mid_size": len(ciphertext),
            },
        }, token)

    # ── HTTP API layer ───────────────────────────────────────────────────

    async def _api_post(
        self,
        endpoint: str,
        body: dict[str, Any],
        timeout_s: int = 15,
        *,
        timeout_is_wait: bool = False,
    ) -> dict:
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
                    log.error("WeChat API %s HTTP %s", endpoint, resp.status)
                    return {"ret": -1, "http_status": resp.status}
                return json.loads(raw)
        except asyncio.TimeoutError:
            if timeout_is_wait:
                log.info("WeChat long-poll timeout after %ss", timeout_s)
                return {"ret": 0, "msgs": []}
            log.error("WeChat API %s timeout after %ss", endpoint, timeout_s)
            raise
        except Exception as e:
            log.error("WeChat API %s error: %s", endpoint, type(e).__name__)
            raise

    async def _weixin_get_updates(self, get_updates_buf: str,
                                  timeout_s: int) -> dict:
        return await self._api_post("ilink/bot/getupdates", {
            "get_updates_buf": get_updates_buf,
        }, timeout_s=timeout_s, timeout_is_wait=True)

    async def _weixin_send_message(self, to: str, text: str,
                                   context_token: str) -> dict:
        return await self._weixin_send_item(
            to, {"type": _ITEM_TEXT, "text_item": {"text": text}}, context_token,
        )

    async def _weixin_send_item(self, to: str, item: dict,
                                context_token: str) -> dict:
        import aiohttp

        if not self._session or self._session_expired or not context_token:
            reason = (
                "session expired" if self._session_expired
                else "session not ready" if not self._session
                else "reply context missing; waiting for an inbound message"
            )
            raise DeliveryError(f"wechat: {reason}", outcome="delivery_unavailable")
        client_id = f"mochi-weixin-{struct.unpack('>I', os.urandom(4))[0]}"
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": client_id,
                "message_type": _MSG_TYPE_BOT,
                "message_state": 2,  # FINISH
                "context_token": context_token,
                "item_list": [item],
            },
            "base_info": {"channel_version": "1.0.0"},
        }
        try:
            response = await self._api_post("ilink/bot/sendmessage", body)
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise DeliveryError(
                f"wechat sendmessage: {type(exc).__name__}; delivery unconfirmed",
                outcome="delivery_unknown",
            ) from exc
        if not isinstance(response, dict):
            raise DeliveryError(
                "wechat sendmessage: invalid response", outcome="delivery_unknown",
            )
        ret = response.get("ret", 0)
        errcode = response.get("errcode", 0)
        http_status = response.get("http_status", 200)
        if ret != 0 or errcode != 0:
            if SESSION_EXPIRED_ERRCODE in (ret, errcode):
                self._invalidate_context_token(to, context_token)
            codes = " ".join(
                f"{name}={value if isinstance(value, int) else 'invalid'}"
                for name, value in (
                    ("ret", ret), ("errcode", errcode), ("http_status", http_status),
                )
            )
            # A gateway error need not mean the downstream send was rejected.
            outcome = (
                "delivery_unknown"
                if not isinstance(http_status, int) or http_status >= 500
                else "delivery_rejected"
            )
            raise DeliveryError(f"wechat sendmessage: {codes}", outcome=outcome)
        return response

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

    async def _download_image(self, image_item: dict) -> ImageAttachment:
        import aiohttp

        media = image_item.get("media") or {}
        query = media.get("encrypt_query_param")
        url = media.get("full_url")
        if not url:
            if not query:
                raise ValueError("Image has no CDN reference")
            url = f"{_CDN_BASE_URL}/download?{urlencode({'encrypted_query_param': query})}"
        _validate_cdn_url(url)

        key = None
        if image_item.get("aeskey"):
            key = bytes.fromhex(image_item["aeskey"])
        elif media.get("aes_key"):
            key = base64.b64decode(media["aes_key"], validate=True)
            if len(key) == 32:
                key = bytes.fromhex(key.decode("ascii"))
        if key is not None and len(key) != 16:
            raise ValueError("Invalid image AES-128 key")

        if self._session is None:
            raise ValueError("WeChat session is not ready")
        # PKCS7 adds up to one AES block to the plaintext size.
        limit = MAX_IMAGE_BYTES + (16 if key is not None else 0)
        async with self._session.get(
            url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ValueError(f"Image CDN returned HTTP {response.status}")
            if response.content_length is not None and response.content_length > limit:
                raise _ImageTooLargeError()
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > limit:
                    raise _ImageTooLargeError()

        plaintext = bytes(data)
        if key is not None:
            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            padded = decryptor.update(plaintext) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            plaintext = unpadder.update(padded) + unpadder.finalize()
        if len(plaintext) > MAX_IMAGE_BYTES:
            raise _ImageTooLargeError()
        return ImageAttachment.from_bytes(plaintext)

    async def _handle_message(self, msg: dict) -> None:
        """Process one inbound WeChat message."""
        from_user = msg.get("from_user_id", "")
        if not from_user:
            return

        if not _is_allowed(from_user):
            log.info("WeChat: rejected message from unlisted user %s",
                     from_user)
            return

        from mochi.heartbeat import active_chat
        with active_chat():
            await self._handle_allowed_message(msg, from_user)

    async def _handle_allowed_message(self, msg: dict, from_user: str) -> None:
        import aiohttp

        items = msg.get("item_list", [])
        text = _extract_text(items)
        images = [item for item in items if item.get("type") == _ITEM_IMAGE]
        # Learn the owner's WeChat ID from the first allowed message
        if self._owner_weixin_id is None and (text or images):
            self._owner_weixin_id = from_user
            log.info("WeChat: owner ID learned: %s", from_user)
            from mochi.db import set_skill_config
            set_skill_config("_transport:wechat", "owner_weixin_id", from_user)

        context_token = msg.get("context_token", "")
        if (
            self._owner_weixin_id is not None
            and isinstance(context_token, str) and context_token
        ):
            self._remember_context_token(from_user, context_token)
        if not text and not images:
            log.info("WeChat: non-text message from %s, skipping", from_user)
            return

        image = None
        if images:
            error_text = ""
            if len(images) > 1:
                error_text = "目前一次只能查看一张图片，请分开发送。"
            else:
                try:
                    image = await self._download_image(images[0].get("image_item") or {})
                except _ImageTooLargeError:
                    log.warning("WeChat: image exceeds size limit")
                    error_text = "图片太大了，请发送 5 MB 以内的图片。"
                except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                    log.warning("WeChat: image download/decode failed (%s)", type(exc).__name__)
                    error_text = "图片下载或解析失败了，请重新发送 JPG、PNG、GIF 或 WebP 图片。"
            if error_text:
                await self._send_text(from_user, error_text, context_token)
                return
            text = text or "用户发来一张图片。"

        command = text.strip() if image is None else ""
        # System command: /restart (owner only)
        if command == "/restart":
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

        # System command: /reset (owner only) — clear conversation context
        if command == "/reset":
            if from_user != self._owner_weixin_id:
                return
            from mochi.db import set_context_reset
            set_context_reset(OWNER_USER_ID or 0)
            try:
                await self._weixin_send_message(
                    from_user, "已重置对话上下文，bot 不会记得之前聊了什么。",
                    context_token)
            except Exception as e:
                log.warning("WeChat: failed to send reset ack: %s", e)
            return

        # System command: /help
        if command == "/help":
            help_text = (
                "我是你的 AI 伙伴，会记住我们的对话，在需要时提醒你。\n\n"
                "直接跟我聊天就行，不用特殊格式。\n\n"
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
            try:
                await self._weixin_send_message(
                    from_user, help_text, context_token)
            except Exception as e:
                log.warning("WeChat: failed to send help: %s", e)
            return

        # System command: /skilloff (owner only)
        if command == "/skilloff":
            if from_user != self._owner_weixin_id:
                return
            from mochi.db import get_skill_mode, set_skill_mode
            if get_skill_mode() == "off":
                msg = "已经是闲聊模式啦~"
            else:
                set_skill_mode("off")
                msg = "已切换到闲聊模式 ✦ 只保留记忆功能，省 token~"
            try:
                await self._weixin_send_message(from_user, msg, context_token)
            except Exception as e:
                log.warning("WeChat: failed to send skilloff ack: %s", e)
            return

        # System command: /skillon (owner only)
        if command == "/skillon":
            if from_user != self._owner_weixin_id:
                return
            from mochi.db import get_skill_mode, set_skill_mode
            if get_skill_mode() == "on":
                msg = "已经是完整模式啦~"
            else:
                set_skill_mode("on")
                msg = "已恢复完整模式 ✦ 所有功能重新上线~"
            try:
                await self._weixin_send_message(from_user, msg, context_token)
            except Exception as e:
                log.warning("WeChat: failed to send skillon ack: %s", e)
            return

        # System command: /heartbeat (owner only)
        if command == "/heartbeat":
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
        if command == "/cost":
            if from_user != self._owner_weixin_id:
                return
            from mochi.db import get_usage_summary
            from mochi.transport.utils import format_usage_summary
            try:
                await self._weixin_send_message(
                    from_user, format_usage_summary(get_usage_summary()), context_token)
            except Exception as e:
                log.warning("WeChat: failed to send cost: %s", e)
            return

        # System command: /core (owner only)
        if command == "/core":
            if from_user != self._owner_weixin_id:
                return
            from mochi.core_store import read_core
            core = read_core()
            reply = f"Core\n\n{core}" if core else "Core 为空。"
            try:
                for start in range(0, len(reply), 4000):
                    await self._weixin_send_message(
                        from_user, reply[start:start + 4000], context_token,
                    )
            except Exception as e:
                log.warning("WeChat: failed to send Core: %s", e)
            return

        # System command: /diary (owner only)
        if command == "/diary":
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
            owner_authorized=from_user == self._owner_weixin_id,
            image=image,
        )

        # Call chat via callback
        if _on_message_callback:
            from mochi.heartbeat import (
                claim_sleep_transition,
                go_to_sleep,
            )
            bedtime_claimed = False

            async def _process_main_turn() -> None:
                nonlocal bedtime_claimed
                try:
                    result = await _on_message_callback(incoming)
                except Exception as e:
                    log.error("WeChat: chat error for %s: %s", from_user, e)
                    result = None

                # Cancel typing
                if typing_ticket:
                    await self._weixin_send_typing(
                        from_user, typing_ticket, status=2)

                if result:
                    if result.bedtime_requested:
                        bedtime_claimed = claim_sleep_transition("explicit")
                        if not bedtime_claimed:
                            return
                    delivered = await self.send_chat_result(
                        user_id,
                        result,
                        context_token=context_token,
                    )
                    if delivered:
                        result.confirm_delivered(final=True)

            try:
                await _process_main_turn()
            finally:
                if bedtime_claimed:
                    go_to_sleep("explicit")
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
                "Retrying in %ds... Re-login: python scripts/weixin_auth.py",
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
                            "python scripts/weixin_auth.py",
                            errcode,
                        )
                        self._session_expired = True
                        for user, token in list(self._context_tokens.items()):
                            self._invalidate_context_token(user, token)
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
