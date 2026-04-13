"""Tests for mochi/transport/weixin.py — WeChat transport."""

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

import mochi.transport.weixin as weixin_mod
from mochi.transport.weixin import (
    WeixinTransport,
    _extract_text,
    _is_allowed,
    _build_headers,
    set_message_handler,
    _ITEM_TEXT,
    _ITEM_IMAGE,
    _ITEM_VOICE,
    _MSG_TYPE_USER,
    SESSION_EXPIRED_ERRCODE,
    WEIXIN_SESSION_EXPIRED_RETRY_S,
)
from mochi.transport import IncomingMessage


# ── Helpers ─────────────────────────────────────────────────────────────────


@dataclass
class FakeChatResult:
    text: str = ""
    stickers: list[str] = field(default_factory=list)


def _text_msg(text: str, from_user: str = "wx_owner_123",
              context_token: str = "ctx-abc") -> dict:
    """Build a minimal WeChat message dict with text content."""
    return {
        "from_user_id": from_user,
        "message_type": _MSG_TYPE_USER,
        "context_token": context_token,
        "item_list": [
            {"type": _ITEM_TEXT, "text_item": {"text": text}},
        ],
    }


# ── Shared fixture ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def weixin_config(monkeypatch):
    """Override WeChat config to safe test defaults."""
    monkeypatch.setattr(weixin_mod, "WEIXIN_BOT_TOKEN", "test-token")
    monkeypatch.setattr(weixin_mod, "WEIXIN_BASE_URL", "https://test.example.com")
    monkeypatch.setattr(weixin_mod, "WEIXIN_ALLOWED_USERS", [])
    monkeypatch.setattr(weixin_mod, "WEIXIN_BUBBLE_DELAY_S", 0)
    monkeypatch.setattr(weixin_mod, "WEIXIN_MSG_LIMIT", 4000)
    monkeypatch.setattr(weixin_mod, "WEIXIN_POLL_TIMEOUT_S", 1)
    monkeypatch.setattr(weixin_mod, "WEIXIN_BACKOFF_MIN_S", 0)
    monkeypatch.setattr(weixin_mod, "WEIXIN_BACKOFF_MAX_S", 0)
    monkeypatch.setattr(weixin_mod, "WEIXIN_MAX_CONSECUTIVE_FAILURES", 3)
    monkeypatch.setattr(weixin_mod, "OWNER_USER_ID", 999)
    # Reset module-level callback between tests
    monkeypatch.setattr(weixin_mod, "_on_message_callback", None)


# ── _extract_text ───────────────────────────────────────────────────────────


class TestExtractText:

    def test_text_item(self):
        items = [{"type": _ITEM_TEXT, "text_item": {"text": "hello"}}]
        assert _extract_text(items) == "hello"

    def test_voice_item(self):
        items = [{"type": _ITEM_VOICE, "voice_item": {"text": "voice msg"}}]
        assert _extract_text(items) == "voice msg"

    def test_ref_msg(self):
        items = [{
            "type": _ITEM_TEXT,
            "text_item": {"text": "reply here"},
            "ref_msg": {"title": "original msg"},
        }]
        assert _extract_text(items) == "[引用「original msg」]\nreply here"

    def test_empty_items(self):
        assert _extract_text([]) == ""

    def test_no_text_type(self):
        items = [{"type": _ITEM_IMAGE, "image_item": {"url": "http://img.png"}}]
        assert _extract_text(items) == ""


# ── _is_allowed ─────────────────────────────────────────────────────────────


class TestIsAllowed:

    def test_empty_allowlist_allows_all(self, monkeypatch):
        monkeypatch.setattr(weixin_mod, "WEIXIN_ALLOWED_USERS", [])
        assert _is_allowed("anyone") is True

    def test_in_list(self, monkeypatch):
        monkeypatch.setattr(weixin_mod, "WEIXIN_ALLOWED_USERS", ["user_a", "user_b"])
        assert _is_allowed("user_a") is True

    def test_not_in_list(self, monkeypatch):
        monkeypatch.setattr(weixin_mod, "WEIXIN_ALLOWED_USERS", ["user_a"])
        assert _is_allowed("user_b") is False


# ── _build_headers ──────────────────────────────────────────────────────────


class TestBuildHeaders:

    def test_has_required_keys(self):
        headers = _build_headers()
        assert headers["Content-Type"] == "application/json"
        assert headers["AuthorizationType"] == "ilink_bot_token"
        assert "X-WECHAT-UIN" in headers

    def test_bearer_token(self, monkeypatch):
        monkeypatch.setattr(weixin_mod, "WEIXIN_BOT_TOKEN", "my-token")
        headers = _build_headers()
        assert headers["Authorization"] == "Bearer my-token"

    def test_no_token(self, monkeypatch):
        monkeypatch.setattr(weixin_mod, "WEIXIN_BOT_TOKEN", "")
        headers = _build_headers()
        assert "Authorization" not in headers


# ── set_message_handler ─────────────────────────────────────────────────────


class TestSetMessageHandler:

    def test_sets_callback(self):
        cb = AsyncMock()
        set_message_handler(cb)
        assert weixin_mod._on_message_callback is cb


# ── WeixinTransport init / properties ───────────────────────────────────────


class TestWeixinTransportInit:

    def test_name(self):
        t = WeixinTransport()
        assert t.name == "wechat"

    def test_initial_state(self):
        t = WeixinTransport()
        assert t._session is None
        assert t._poll_task is None
        assert t._stopped is False
        assert t._session_expired is False
        assert t.session_expired is False
        assert t._owner_weixin_id is None
        assert t._context_tokens == {}
        assert t._typing_tickets == {}


# ── send_message ────────────────────────────────────────────────────────────


class TestSendMessage:

    @pytest.mark.asyncio
    async def test_send_no_session(self):
        """No session → warning, no crash."""
        t = WeixinTransport()
        t._owner_weixin_id = "wx_123"
        # _session is None
        await t.send_message(999, "hello")  # should not raise

    @pytest.mark.asyncio
    async def test_send_no_owner_id(self):
        """No owner learned yet → warning, no crash."""
        t = WeixinTransport()
        t._session = MagicMock()
        # _owner_weixin_id is None
        await t.send_message(999, "hello")  # should not raise

    @pytest.mark.asyncio
    async def test_send_cleans_markers(self):
        """Side-channel markers are stripped before sending."""
        t = WeixinTransport()
        t._session = MagicMock()
        t._owner_weixin_id = "wx_123"
        t._weixin_send_message = AsyncMock(return_value={"ret": 0})

        await t.send_message(999, "[STICKER:happy] actual text")

        t._weixin_send_message.assert_called_once()
        sent_text = t._weixin_send_message.call_args[0][1]
        assert "[STICKER:" not in sent_text
        assert "actual text" in sent_text

    @pytest.mark.asyncio
    async def test_send_splits_bubbles(self):
        """Long text with delimiter splits into multiple API calls."""
        t = WeixinTransport()
        t._session = MagicMock()
        t._owner_weixin_id = "wx_123"
        t._weixin_send_message = AsyncMock(return_value={"ret": 0})

        await t.send_message(999, "long bubble one ||| long bubble two")

        assert t._weixin_send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_send_api_error(self):
        """API exception is caught and logged, not raised."""
        t = WeixinTransport()
        t._session = MagicMock()
        t._owner_weixin_id = "wx_123"
        t._weixin_send_message = AsyncMock(side_effect=Exception("network"))

        await t.send_message(999, "hello world test")  # should not raise

    @pytest.mark.asyncio
    async def test_send_empty_after_clean(self):
        """Message that becomes empty after marker cleaning is not sent."""
        t = WeixinTransport()
        t._session = MagicMock()
        t._owner_weixin_id = "wx_123"
        t._weixin_send_message = AsyncMock()

        await t.send_message(999, "[SKIP]")

        t._weixin_send_message.assert_not_called()


# ── _handle_message ─────────────────────────────────────────────────────────


class TestHandleMessage:

    @pytest.fixture
    def transport(self):
        t = WeixinTransport()
        t._session = MagicMock()
        # Mock API calls that _handle_message triggers
        t._api_post = AsyncMock(return_value={"ret": 0, "typing_ticket": ""})
        t._weixin_send_typing = AsyncMock()
        t._weixin_send_message = AsyncMock(return_value={"ret": 0})
        # Suppress heartbeat dispatch
        t._dispatch_state_signals = MagicMock()
        return t

    @pytest.mark.asyncio
    async def test_rejects_empty_from_user(self, transport):
        msg = {"from_user_id": "", "item_list": []}
        await transport._handle_message(msg)
        # No crash, no callback

    @pytest.mark.asyncio
    async def test_rejects_unlisted_user(self, transport, monkeypatch):
        monkeypatch.setattr(weixin_mod, "WEIXIN_ALLOWED_USERS", ["allowed_user"])
        msg = _text_msg("hi", from_user="unlisted_user")
        await transport._handle_message(msg)
        # No callback invoked, no owner learned
        assert transport._owner_weixin_id is None

    @pytest.mark.asyncio
    async def test_skips_non_text(self, transport):
        msg = {
            "from_user_id": "wx_123",
            "context_token": "ctx",
            "item_list": [{"type": _ITEM_IMAGE, "image_item": {"url": "http://img"}}],
        }
        await transport._handle_message(msg)
        assert transport._owner_weixin_id is None  # never got past text extraction

    @pytest.mark.asyncio
    async def test_caches_context_token(self, transport):
        msg = _text_msg("hello there", context_token="my-token-123")
        await transport._handle_message(msg)
        assert transport._context_tokens["wx_owner_123"] == "my-token-123"

    @pytest.mark.asyncio
    async def test_learns_owner_id(self, transport):
        assert transport._owner_weixin_id is None
        msg = _text_msg("hello there", from_user="wx_new_owner")
        await transport._handle_message(msg)
        assert transport._owner_weixin_id == "wx_new_owner"

    @pytest.mark.asyncio
    async def test_calls_callback(self, transport, monkeypatch):
        captured = []

        async def fake_callback(incoming):
            captured.append(incoming)
            return FakeChatResult(text="")

        monkeypatch.setattr(weixin_mod, "_on_message_callback", fake_callback)
        msg = _text_msg("test message", from_user="wx_user")
        await transport._handle_message(msg)

        assert len(captured) == 1
        assert isinstance(captured[0], IncomingMessage)
        assert captured[0].user_id == 999
        assert captured[0].text == "test message"
        assert captured[0].transport == "wechat"
        assert captured[0].raw == {"weixin_user_id": "wx_user"}

    @pytest.mark.asyncio
    async def test_sends_reply_after_callback(self, transport, monkeypatch):
        async def fake_callback(incoming):
            return FakeChatResult(text="my reply text here")

        monkeypatch.setattr(weixin_mod, "_on_message_callback", fake_callback)
        msg = _text_msg("user says hi")
        await transport._handle_message(msg)

        transport._weixin_send_message.assert_called()
        sent_text = transport._weixin_send_message.call_args[0][1]
        assert "my reply text here" in sent_text

    @pytest.mark.asyncio
    async def test_typing_sent_before_callback(self, transport, monkeypatch):
        """Typing indicator is sent before the callback processes."""
        # Pre-cache a typing ticket
        transport._typing_tickets["wx_user"] = "ticket-abc"

        call_order = []

        async def fake_callback(incoming):
            call_order.append("callback")
            return FakeChatResult(text="reply")

        async def track_typing(user_id, ticket, status=1):
            call_order.append(("typing", status))

        transport._weixin_send_typing = track_typing
        monkeypatch.setattr(weixin_mod, "_on_message_callback", fake_callback)

        msg = _text_msg("hi", from_user="wx_user")
        await transport._handle_message(msg)

        # typing(1) should come before callback
        assert call_order[0] == ("typing", 1)
        assert "callback" in call_order

    @pytest.mark.asyncio
    async def test_typing_cancelled_after_callback(self, transport, monkeypatch):
        """Typing cancel (status=2) sent after callback completes."""
        transport._typing_tickets["wx_user"] = "ticket-abc"
        typing_calls = []

        async def track_typing(user_id, ticket, status=1):
            typing_calls.append(status)

        transport._weixin_send_typing = track_typing

        async def fake_callback(incoming):
            return FakeChatResult(text="reply")

        monkeypatch.setattr(weixin_mod, "_on_message_callback", fake_callback)
        msg = _text_msg("hi", from_user="wx_user")
        await transport._handle_message(msg)

        # Should have status=1 (start) and status=2 (cancel)
        assert 1 in typing_calls
        assert 2 in typing_calls

    @pytest.mark.asyncio
    async def test_typing_cancelled_even_without_callback(self, transport):
        """Typing cancel sent even when no callback is registered."""
        transport._typing_tickets["wx_user"] = "ticket-abc"
        typing_calls = []

        async def track_typing(user_id, ticket, status=1):
            typing_calls.append(status)

        transport._weixin_send_typing = track_typing
        # _on_message_callback is None (autouse fixture)

        msg = _text_msg("hi", from_user="wx_user")
        await transport._handle_message(msg)

        assert 2 in typing_calls

    @pytest.mark.asyncio
    async def test_callback_exception(self, transport, monkeypatch):
        """Callback raising an exception is caught gracefully."""
        async def bad_callback(incoming):
            raise RuntimeError("boom")

        monkeypatch.setattr(weixin_mod, "_on_message_callback", bad_callback)
        msg = _text_msg("hi")
        await transport._handle_message(msg)  # should not raise

    @pytest.mark.asyncio
    async def test_check_sleep_entry_called(self, transport, monkeypatch):
        """check_sleep_entry is called with the user's text after reply."""
        called_with = []

        monkeypatch.setattr(
            "mochi.heartbeat.check_sleep_entry",
            lambda text: called_with.append(text) or False,
        )

        async def fake_callback(incoming):
            return FakeChatResult(text="good night!")

        monkeypatch.setattr(weixin_mod, "_on_message_callback", fake_callback)
        msg = _text_msg("晚安")
        await transport._handle_message(msg)

        assert "晚安" in called_with


# ── System commands ────────────────────────────────────────────────────────


class TestSystemCommands:
    """Tests for /help /heartbeat /cost /notes /diary commands in WeChat."""

    @pytest.fixture
    def transport(self):
        t = WeixinTransport()
        t._session = MagicMock()
        t._api_post = AsyncMock(return_value={"ret": 0, "typing_ticket": ""})
        t._weixin_send_typing = AsyncMock()
        t._weixin_send_message = AsyncMock(return_value={"ret": 0})
        t._dispatch_state_signals = MagicMock()
        return t

    @pytest.mark.asyncio
    async def test_help_returns_command_list(self, transport):
        msg = _text_msg("/help")
        await transport._handle_message(msg)

        transport._weixin_send_message.assert_called_once()
        text = transport._weixin_send_message.call_args[0][1]
        for cmd in ["/help", "/heartbeat", "/cost", "/notes", "/diary", "/restart"]:
            assert cmd in text

    @pytest.mark.asyncio
    async def test_help_does_not_enter_chat(self, transport, monkeypatch):
        """Help command should return early, not trigger AI chat callback."""
        callback = AsyncMock()
        monkeypatch.setattr(weixin_mod, "_on_message_callback", callback)
        msg = _text_msg("/help")
        await transport._handle_message(msg)

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_heartbeat_owner_only(self, transport, monkeypatch):
        """Non-owner cannot use /heartbeat."""
        transport._owner_weixin_id = "wx_real_owner"

        msg = _text_msg("/heartbeat", from_user="wx_intruder")
        await transport._handle_message(msg)

        # Should not send any message (silently ignored)
        transport._weixin_send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_heartbeat_shows_stats(self, transport, monkeypatch):
        transport._owner_weixin_id = "wx_owner_123"
        stats = {
            "state": "AWAKE",
            "proactive_today": 3,
            "proactive_limit": 5,
            "last_think_at": "2026-04-13T10:00:00",
        }
        monkeypatch.setattr("mochi.heartbeat.get_stats", lambda: stats)
        monkeypatch.setattr("mochi.db.get_last_heartbeat_log", lambda: None)

        msg = _text_msg("/heartbeat")
        await transport._handle_message(msg)

        text = transport._weixin_send_message.call_args[0][1]
        assert "AWAKE" in text
        assert "3/5" in text

    @pytest.mark.asyncio
    async def test_heartbeat_with_log_entry(self, transport, monkeypatch):
        transport._owner_weixin_id = "wx_owner_123"
        stats = {
            "state": "SLEEPING",
            "proactive_today": 0,
            "proactive_limit": 5,
            "last_think_at": None,
        }
        entry = {
            "created_at": "2026-04-13T09:00:00",
            "state": "AWAKE",
            "action": "think",
            "summary": "Checked reminders",
        }
        monkeypatch.setattr("mochi.heartbeat.get_stats", lambda: stats)
        monkeypatch.setattr("mochi.db.get_last_heartbeat_log", lambda: entry)

        msg = _text_msg("/heartbeat")
        await transport._handle_message(msg)

        text = transport._weixin_send_message.call_args[0][1]
        assert "最近一次心跳" in text
        assert "Checked reminders" in text

    @pytest.mark.asyncio
    async def test_cost_owner_only(self, transport):
        transport._owner_weixin_id = "wx_real_owner"

        msg = _text_msg("/cost", from_user="wx_intruder")
        await transport._handle_message(msg)

        transport._weixin_send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_cost_shows_usage(self, transport, monkeypatch):
        transport._owner_weixin_id = "wx_owner_123"
        summary = {
            "today": {"by_model": {
                "claude-3": {"prompt": 1000, "completion": 500},
            }},
            "month": {"by_model": {
                "claude-3": {"prompt": 10000, "completion": 5000},
            }},
        }
        monkeypatch.setattr("mochi.db.get_usage_summary", lambda: summary)

        msg = _text_msg("/cost")
        await transport._handle_message(msg)

        text = transport._weixin_send_message.call_args[0][1]
        assert "claude-3" in text
        assert "1,000" in text

    @pytest.mark.asyncio
    async def test_notes_owner_only(self, transport):
        transport._owner_weixin_id = "wx_real_owner"

        msg = _text_msg("/notes", from_user="wx_intruder")
        await transport._handle_message(msg)

        transport._weixin_send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_notes_shows_list(self, transport):
        transport._owner_weixin_id = "wx_owner_123"

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value=(
                 "# Notes\n\n## Notes\n"
                 "- Buy groceries (2026-04-12)\n"
                 "- Call dentist (2026-04-13)\n"
             )):
            msg = _text_msg("/notes")
            await transport._handle_message(msg)

        text = transport._weixin_send_message.call_args[0][1]
        assert "📝 Notes" in text
        assert "1. Buy groceries (2026-04-12)" in text
        assert "2. Call dentist (2026-04-13)" in text

    @pytest.mark.asyncio
    async def test_notes_empty(self, transport):
        transport._owner_weixin_id = "wx_owner_123"

        with patch("pathlib.Path.exists", return_value=False):
            msg = _text_msg("/notes")
            await transport._handle_message(msg)

        text = transport._weixin_send_message.call_args[0][1]
        assert text == "No notes."

    @pytest.mark.asyncio
    async def test_diary_owner_only(self, transport):
        transport._owner_weixin_id = "wx_real_owner"

        msg = _text_msg("/diary", from_user="wx_intruder")
        await transport._handle_message(msg)

        transport._weixin_send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_diary_shows_sections(self, transport, monkeypatch):
        transport._owner_weixin_id = "wx_owner_123"
        monkeypatch.setattr(
            "mochi.diary.diary.read",
            lambda section=None: {
                "今日状態": "- habit1: done",
                "今日日記": "- [10:00] had coffee",
            }.get(section, ""),
        )
        monkeypatch.setattr("mochi.config.logical_today", lambda: "2026-04-13")

        msg = _text_msg("/diary")
        await transport._handle_message(msg)

        text = transport._weixin_send_message.call_args[0][1]
        assert "📖 今日日記 (2026-04-13)" in text
        assert "今日状態" in text
        assert "habit1: done" in text
        assert "had coffee" in text

    @pytest.mark.asyncio
    async def test_diary_empty_sections(self, transport, monkeypatch):
        transport._owner_weixin_id = "wx_owner_123"
        monkeypatch.setattr(
            "mochi.diary.diary.read",
            lambda section=None: "",
        )
        monkeypatch.setattr("mochi.config.logical_today", lambda: "2026-04-13")

        msg = _text_msg("/diary")
        await transport._handle_message(msg)

        text = transport._weixin_send_message.call_args[0][1]
        assert "(无)" in text

    @pytest.mark.asyncio
    async def test_commands_do_not_trigger_chat(self, transport, monkeypatch):
        """All system commands should return before hitting the AI chat flow."""
        callback = AsyncMock()
        monkeypatch.setattr(weixin_mod, "_on_message_callback", callback)

        for cmd in ["/help", "/restart"]:
            transport._weixin_send_message.reset_mock()
            callback.reset_mock()
            msg = _text_msg(cmd)
            await transport._handle_message(msg)
            callback.assert_not_called()


# ── _poll_loop ──────────────────────────────────────────────────────────────


class TestPollLoop:

    @pytest.mark.asyncio
    async def test_session_expired_stops_loop(self, monkeypatch):
        """errcode -14 makes the poll loop exit and sets session_expired flag."""
        t = WeixinTransport()
        t._session = MagicMock()

        async def fake_get_updates(buf, timeout):
            return {"ret": 0, "errcode": SESSION_EXPIRED_ERRCODE, "msgs": []}

        t._weixin_get_updates = fake_get_updates
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        assert t._session_expired is False
        # Should return (not hang forever)
        await t._poll_loop()
        assert t._session_expired is True

    @pytest.mark.asyncio
    async def test_filters_user_messages_only(self, monkeypatch):
        """Only message_type=1 (user) messages are dispatched."""
        t = WeixinTransport()
        t._session = MagicMock()
        handled = []
        call_count = 0

        async def fake_get_updates(buf, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "ret": 0, "errcode": 0,
                    "msgs": [
                        {"message_type": 2, "from_user_id": "bot"},  # bot msg
                        {"message_type": _MSG_TYPE_USER, "from_user_id": "u1",
                         "item_list": [{"type": _ITEM_TEXT, "text_item": {"text": "hi"}}],
                         "context_token": "ctx"},
                    ],
                    "get_updates_buf": "buf1",
                }
            # Let tasks run before exiting
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        t._weixin_get_updates = fake_get_updates

        async def tracking_handle(msg):
            handled.append(msg.get("from_user_id"))

        t._handle_message = tracking_handle

        await t._poll_loop()

        # Only the user message should have been handled
        assert "u1" in handled
        assert "bot" not in handled

    @pytest.mark.asyncio
    async def test_consecutive_failures_backoff(self, monkeypatch):
        """Failures increment counter and trigger sleep."""
        t = WeixinTransport()
        t._session = MagicMock()
        sleep_calls = []
        call_count = 0

        async def fake_get_updates(buf, timeout):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return {"ret": -1, "errcode": 99, "msgs": []}
            raise asyncio.CancelledError

        t._weixin_get_updates = fake_get_updates

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await t._poll_loop()

        # Should have slept 3 times (once per failure)
        assert len(sleep_calls) == 3

    @pytest.mark.asyncio
    async def test_cursor_updated(self, monkeypatch):
        """get_updates_buf from response is used in next poll."""
        t = WeixinTransport()
        t._session = MagicMock()
        buf_seen = []
        call_count = 0

        async def fake_get_updates(buf, timeout):
            nonlocal call_count
            call_count += 1
            buf_seen.append(buf)
            if call_count == 1:
                return {"ret": 0, "errcode": 0, "msgs": [],
                        "get_updates_buf": "cursor-2"}
            if call_count == 2:
                return {"ret": 0, "errcode": 0, "msgs": [],
                        "get_updates_buf": "cursor-3"}
            raise asyncio.CancelledError

        t._weixin_get_updates = fake_get_updates
        await t._poll_loop()

        assert buf_seen == ["", "cursor-2", "cursor-3"]

    @pytest.mark.asyncio
    async def test_cancelled_error_breaks_loop(self):
        """CancelledError exits the loop cleanly."""
        t = WeixinTransport()
        t._session = MagicMock()

        async def fake_get_updates(buf, timeout):
            raise asyncio.CancelledError

        t._weixin_get_updates = fake_get_updates
        await t._poll_loop()  # should return without raising


# ── _dispatch_state_signals ─────────────────────────────────────────────────


class TestDispatchStateSignals:

    def test_wake_up_when_sleeping(self, monkeypatch):
        wake_calls = []
        monkeypatch.setattr("mochi.heartbeat.should_wake_on_message", lambda: True)
        monkeypatch.setattr("mochi.heartbeat.wake_up",
                            lambda reason: wake_calls.append(reason))
        monkeypatch.setattr("mochi.heartbeat.clear_morning_hold", lambda: None)
        monkeypatch.setattr("mochi.heartbeat.clear_silent_pause", lambda: None)

        WeixinTransport._dispatch_state_signals()
        assert wake_calls == ["user_message"]

    def test_clears_holds(self, monkeypatch):
        cleared = []
        monkeypatch.setattr("mochi.heartbeat.should_wake_on_message", lambda: False)
        monkeypatch.setattr("mochi.heartbeat.wake_up", lambda r: None)
        monkeypatch.setattr("mochi.heartbeat.clear_morning_hold",
                            lambda: cleared.append("morning"))
        monkeypatch.setattr("mochi.heartbeat.clear_silent_pause",
                            lambda: cleared.append("silent"))

        WeixinTransport._dispatch_state_signals()
        assert "morning" in cleared
        assert "silent" in cleared

    def test_no_wake_when_awake(self, monkeypatch):
        wake_calls = []
        monkeypatch.setattr("mochi.heartbeat.should_wake_on_message", lambda: False)
        monkeypatch.setattr("mochi.heartbeat.wake_up",
                            lambda reason: wake_calls.append(reason))
        monkeypatch.setattr("mochi.heartbeat.clear_morning_hold", lambda: None)
        monkeypatch.setattr("mochi.heartbeat.clear_silent_pause", lambda: None)

        WeixinTransport._dispatch_state_signals()
        assert wake_calls == []


# ── _supervised_poll_loop ──────────────────────────────────────────────────


class TestSupervisedPollLoop:

    @pytest.mark.asyncio
    async def test_restarts_after_session_expiry(self, monkeypatch):
        """Supervisor restarts poll_loop after session expiry + successful probe."""
        t = WeixinTransport()
        t._session = MagicMock()
        monkeypatch.setattr(weixin_mod, "WEIXIN_SESSION_EXPIRED_RETRY_S", 0)
        poll_calls = 0

        async def fake_poll_loop(self_arg=None):
            nonlocal poll_calls
            poll_calls += 1
            if poll_calls == 1:
                t._session_expired = True
                return  # first run: session expired
            # second run: simulate normal CancelledError exit
            raise asyncio.CancelledError

        t._poll_loop = lambda: fake_poll_loop()

        # Probe returns success (no expiry errcode)
        async def fake_get_updates(buf, timeout_s):
            return {"ret": 0, "errcode": 0, "msgs": []}

        t._weixin_get_updates = fake_get_updates
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        # CancelledError from second poll_loop should propagate
        with pytest.raises(asyncio.CancelledError):
            await t._supervised_poll_loop()

        assert poll_calls == 2
        assert t._session_expired is False

    @pytest.mark.asyncio
    async def test_keeps_retrying_while_expired(self, monkeypatch):
        """Supervisor retries probe multiple times before recovery."""
        t = WeixinTransport()
        t._session = MagicMock()
        monkeypatch.setattr(weixin_mod, "WEIXIN_SESSION_EXPIRED_RETRY_S", 0)
        probe_calls = 0
        poll_calls = 0

        async def fake_poll_loop():
            nonlocal poll_calls
            poll_calls += 1
            if poll_calls == 1:
                t._session_expired = True
                return
            # Second call after recovery — exit cleanly
            return

        t._poll_loop = fake_poll_loop

        async def fake_get_updates(buf, timeout_s):
            nonlocal probe_calls
            probe_calls += 1
            if probe_calls <= 2:
                # Still expired
                return {"ret": 0, "errcode": SESSION_EXPIRED_ERRCODE, "msgs": []}
            # Recovered
            return {"ret": 0, "errcode": 0, "msgs": []}

        t._weixin_get_updates = fake_get_updates
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await t._supervised_poll_loop()

        assert probe_calls == 3
        assert poll_calls == 2
        assert t._session_expired is False

    @pytest.mark.asyncio
    async def test_exits_on_non_expiry(self, monkeypatch):
        """Supervisor exits when poll_loop exits without session_expired."""
        t = WeixinTransport()
        t._session = MagicMock()

        async def fake_poll_loop():
            return  # exit without setting _session_expired

        t._poll_loop = fake_poll_loop

        await t._supervised_poll_loop()
        assert t._session_expired is False

    @pytest.mark.asyncio
    async def test_exits_on_stopped(self, monkeypatch):
        """Supervisor exits when _stopped is set."""
        t = WeixinTransport()
        t._session = MagicMock()
        t._stopped = True

        async def fake_poll_loop():
            return

        t._poll_loop = fake_poll_loop

        await t._supervised_poll_loop()

    @pytest.mark.asyncio
    async def test_probe_exception_retries(self, monkeypatch):
        """Supervisor retries if probe raises an exception."""
        t = WeixinTransport()
        t._session = MagicMock()
        monkeypatch.setattr(weixin_mod, "WEIXIN_SESSION_EXPIRED_RETRY_S", 0)
        probe_calls = 0
        poll_calls = 0

        async def fake_poll_loop():
            nonlocal poll_calls
            poll_calls += 1
            if poll_calls == 1:
                t._session_expired = True
                return
            # Second call after recovery — exit cleanly
            return

        t._poll_loop = fake_poll_loop

        async def fake_get_updates(buf, timeout_s):
            nonlocal probe_calls
            probe_calls += 1
            if probe_calls == 1:
                raise ConnectionError("network down")
            # Recovered
            return {"ret": 0, "errcode": 0, "msgs": []}

        t._weixin_get_updates = fake_get_updates
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await t._supervised_poll_loop()

        assert probe_calls == 2
        assert poll_calls == 2
        assert t._session_expired is False
