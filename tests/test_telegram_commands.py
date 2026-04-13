"""Tests for Telegram transport command handlers."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

import mochi.transport.telegram as tg_mod
from mochi.transport.telegram import TelegramTransport, _is_owner


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_update(user_id: int = 999, text: str = "") -> MagicMock:
    """Build a minimal Telegram Update mock."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


@pytest.fixture(autouse=True)
def tg_config(monkeypatch):
    """Set OWNER_USER_ID so _is_owner passes for user 999."""
    monkeypatch.setattr("mochi.transport.telegram.OWNER_USER_ID", 999)
    # Also patch the dynamic import inside _is_owner
    monkeypatch.setattr("mochi.config.OWNER_USER_ID", 999)


# ── _cmd_help ──────────────────────────────────────────────────────────────


class TestCmdHelp:

    @pytest.mark.asyncio
    async def test_help_lists_all_commands(self):
        t = TelegramTransport()
        update = _make_update()
        await t._cmd_help(update, None)

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        for cmd in ["/help", "/heartbeat", "/cost", "/notes", "/diary", "/restart"]:
            assert cmd in text


# ── _cmd_heartbeat ─────────────────────────────────────────────────────────


class TestCmdHeartbeat:

    @pytest.mark.asyncio
    async def test_owner_gets_stats(self, monkeypatch):
        stats = {
            "state": "AWAKE",
            "proactive_today": 2,
            "proactive_limit": 5,
            "last_think_at": "2026-04-13T10:00:00",
        }
        monkeypatch.setattr("mochi.heartbeat.get_stats", lambda: stats)
        monkeypatch.setattr("mochi.db.get_last_heartbeat_log", lambda: None)

        t = TelegramTransport()
        update = _make_update(user_id=999)
        await t._cmd_heartbeat(update, None)

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "AWAKE" in text
        assert "2/5" in text

    @pytest.mark.asyncio
    async def test_includes_heartbeat_log(self, monkeypatch):
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

        t = TelegramTransport()
        update = _make_update(user_id=999)
        await t._cmd_heartbeat(update, None)

        text = update.message.reply_text.call_args[0][0]
        assert "最近一次心跳" in text
        assert "Checked reminders" in text

    @pytest.mark.asyncio
    async def test_truncates_long_summary(self, monkeypatch):
        stats = {
            "state": "AWAKE",
            "proactive_today": 0,
            "proactive_limit": 5,
            "last_think_at": None,
        }
        entry = {
            "created_at": "2026-04-13T09:00:00",
            "state": "AWAKE",
            "action": "think",
            "summary": "x" * 700,
        }
        monkeypatch.setattr("mochi.heartbeat.get_stats", lambda: stats)
        monkeypatch.setattr("mochi.db.get_last_heartbeat_log", lambda: entry)

        t = TelegramTransport()
        update = _make_update(user_id=999)
        await t._cmd_heartbeat(update, None)

        text = update.message.reply_text.call_args[0][0]
        assert "…(截断)" in text
        # Summary should be truncated to ~600 chars + truncation marker
        assert len(text) < 800

    @pytest.mark.asyncio
    async def test_non_owner_ignored(self, monkeypatch):
        t = TelegramTransport()
        update = _make_update(user_id=12345)  # not owner
        await t._cmd_heartbeat(update, None)

        update.message.reply_text.assert_not_called()


# ── _cmd_cost ──────────────────────────────────────────────────────────────


class TestCmdCost:

    @pytest.mark.asyncio
    async def test_formats_usage(self, monkeypatch):
        summary = {
            "today": {"by_model": {
                "claude-3": {"prompt": 1000, "completion": 500},
            }},
            "month": {"by_model": {
                "claude-3": {"prompt": 10000, "completion": 5000},
            }},
        }
        monkeypatch.setattr("mochi.db.get_usage_summary", lambda: summary)

        t = TelegramTransport()
        update = _make_update(user_id=999)
        await t._cmd_cost(update, None)

        text = update.message.reply_text.call_args[0][0]
        assert "claude-3" in text
        assert "1,000" in text
        assert "10,000" in text

    @pytest.mark.asyncio
    async def test_empty_usage(self, monkeypatch):
        summary = {
            "today": {"by_model": {}},
            "month": {"by_model": {}},
        }
        monkeypatch.setattr("mochi.db.get_usage_summary", lambda: summary)

        t = TelegramTransport()
        update = _make_update(user_id=999)
        await t._cmd_cost(update, None)

        text = update.message.reply_text.call_args[0][0]
        assert "(无记录)" in text

    @pytest.mark.asyncio
    async def test_non_owner_ignored(self, monkeypatch):
        t = TelegramTransport()
        update = _make_update(user_id=12345)
        await t._cmd_cost(update, None)

        update.message.reply_text.assert_not_called()


# ── _cmd_notes ─────────────────────────────────────────────────────────────


class TestCmdNotes:

    @pytest.mark.asyncio
    async def test_shows_notes(self, tmp_path, monkeypatch):
        notes_file = tmp_path / "notes.md"
        notes_file.write_text(
            "# Notes\n\n## Notes\n"
            "- Buy groceries (2026-04-12)\n"
            "- Call dentist (2026-04-13)\n",
            encoding="utf-8",
        )
        # Patch the path resolution inside _cmd_notes
        monkeypatch.setattr(
            "pathlib.Path.resolve",
            lambda self: tmp_path / "mochi" / "transport" / "telegram.py",
        )
        # Easier: just patch Path(__file__) chain — instead, patch at method level
        # Actually, let's just patch the entire method's Path usage
        import pathlib
        original_path = pathlib.Path

        class PatchedPath(type(original_path())):
            pass

        # Simpler approach: mock the file read directly
        t = TelegramTransport()
        update = _make_update(user_id=999)

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value=(
                 "# Notes\n\n## Notes\n"
                 "- Buy groceries (2026-04-12)\n"
                 "- Call dentist (2026-04-13)\n"
             )):
            await t._cmd_notes(update, None)

        text = update.message.reply_text.call_args[0][0]
        assert "📝 Notes" in text
        assert "1. Buy groceries (2026-04-12)" in text
        assert "2. Call dentist (2026-04-13)" in text

    @pytest.mark.asyncio
    async def test_no_notes(self):
        t = TelegramTransport()
        update = _make_update(user_id=999)

        with patch("pathlib.Path.exists", return_value=False):
            await t._cmd_notes(update, None)

        text = update.message.reply_text.call_args[0][0]
        assert text == "No notes."

    @pytest.mark.asyncio
    async def test_empty_file(self):
        t = TelegramTransport()
        update = _make_update(user_id=999)

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value="# Notes\n\n## Notes\n"):
            await t._cmd_notes(update, None)

        text = update.message.reply_text.call_args[0][0]
        assert text == "No notes."

    @pytest.mark.asyncio
    async def test_non_owner_ignored(self):
        t = TelegramTransport()
        update = _make_update(user_id=12345)
        await t._cmd_notes(update, None)
        update.message.reply_text.assert_not_called()


# ── _cmd_diary ─────────────────────────────────────────────────────────────


class TestCmdDiary:

    @pytest.mark.asyncio
    async def test_shows_diary(self, monkeypatch):
        monkeypatch.setattr(
            "mochi.diary.diary.read",
            lambda section=None: {
                "今日状態": "- habit1: done",
                "今日日記": "- [10:00] had coffee",
            }.get(section, ""),
        )
        monkeypatch.setattr("mochi.config.logical_today", lambda: "2026-04-13")

        t = TelegramTransport()
        update = _make_update(user_id=999)
        await t._cmd_diary(update, None)

        text = update.message.reply_text.call_args[0][0]
        assert "📖 今日日記 (2026-04-13)" in text
        assert "今日状態" in text
        assert "habit1: done" in text
        assert "今日日記" in text
        assert "had coffee" in text

    @pytest.mark.asyncio
    async def test_empty_sections(self, monkeypatch):
        monkeypatch.setattr(
            "mochi.diary.diary.read",
            lambda section=None: "",
        )
        monkeypatch.setattr("mochi.config.logical_today", lambda: "2026-04-13")

        t = TelegramTransport()
        update = _make_update(user_id=999)
        await t._cmd_diary(update, None)

        text = update.message.reply_text.call_args[0][0]
        # Empty sections should show "(无)"
        assert "(无)" in text

    @pytest.mark.asyncio
    async def test_non_owner_ignored(self):
        t = TelegramTransport()
        update = _make_update(user_id=12345)
        await t._cmd_diary(update, None)
        update.message.reply_text.assert_not_called()
