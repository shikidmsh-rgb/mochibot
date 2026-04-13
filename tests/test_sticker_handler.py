"""Tests for the sticker skill handler — send, learn, delete, tag generation."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from mochi.skills.base import SkillContext, SkillResult
from mochi.skills.sticker.handler import (
    StickerSkill,
    generate_sticker_tags,
    record_last_sent_sticker,
    get_last_sent_sticker,
    _last_sent_sticker,
)


@pytest.fixture(autouse=True)
def clear_sticker_cache():
    """Clear the in-memory last-sent sticker cache between tests."""
    _last_sent_sticker.clear()
    yield
    _last_sent_sticker.clear()


class TestGenerateStickerTags:

    @pytest.mark.asyncio
    async def test_success(self):
        """LLM returns valid tags."""
        mock_client = MagicMock()
        mock_client.chat.return_value = MagicMock(content="开心,撒娇,晚安")
        with patch("mochi.llm.get_client_for_tier", return_value=mock_client):
            tags = await generate_sticker_tags("😊", "happy_set", "好开心")
        assert "开心" in tags
        assert "撒娇" in tags
        mock_client.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_returns_empty_falls_back_to_emoji(self):
        """LLM returns empty string -> fallback to emoji."""
        mock_client = MagicMock()
        mock_client.chat.return_value = MagicMock(content="")
        with patch("mochi.llm.get_client_for_tier", return_value=mock_client):
            tags = await generate_sticker_tags("😊", "set", "")
        assert tags == "😊"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_emoji(self):
        """LLM raises exception -> fallback to emoji."""
        with patch("mochi.llm.get_client_for_tier", side_effect=Exception("API error")):
            tags = await generate_sticker_tags("😭", "sad_set", "难过")
        assert tags == "😭"

    @pytest.mark.asyncio
    async def test_llm_failure_no_emoji_falls_back_to_sticker(self):
        """LLM raises exception and emoji is empty -> fallback to 'sticker'."""
        with patch("mochi.llm.get_client_for_tier", side_effect=Exception("fail")):
            tags = await generate_sticker_tags("", "set", "")
        assert tags == "sticker"


class TestStickerSkillExecute:

    def _make_context(self, tool_name, args=None, user_id=1, channel_id=100):
        return SkillContext(
            trigger="tool_call",
            user_id=user_id,
            channel_id=channel_id,
            tool_name=tool_name,
            args=args or {},
        )

    @pytest.mark.asyncio
    async def test_send_no_mood_error(self):
        """send_sticker with empty mood returns error."""
        ctx = self._make_context("send_sticker", {"mood": ""})
        result = await StickerSkill().execute(ctx)
        assert not result.success
        assert "mood" in result.output.lower() or "specify" in result.output.lower()

    @pytest.mark.asyncio
    async def test_send_no_stickers_learned(self):
        """send_sticker when no stickers exist returns guidance message."""
        ctx = self._make_context("send_sticker", {"mood": "开心"})
        with patch("mochi.db.get_sticker_count", return_value=0):
            result = await StickerSkill().execute(ctx)
        assert "No stickers" in result.output or "no stickers" in result.output.lower()

    @pytest.mark.asyncio
    async def test_send_exact_tag_match(self):
        """send_sticker finds sticker by exact tag match."""
        sticker = {"file_id": "ABC123", "tags": "开心,快乐", "emoji": "😊"}
        ctx = self._make_context("send_sticker", {"mood": "开心"})
        with patch("mochi.db.get_sticker_count", return_value=5), \
             patch("mochi.db.get_stickers_by_tag", return_value=[sticker]):
            result = await StickerSkill().execute(ctx)
        assert "STICKER:ABC123" in result.output
        assert result.success

    @pytest.mark.asyncio
    async def test_send_fallback_to_random(self):
        """send_sticker falls back to random when no tag matches."""
        sticker = {"file_id": "RAND1", "tags": "无关", "emoji": "🤔"}
        ctx = self._make_context("send_sticker", {"mood": "xyz"})
        with patch("mochi.db.get_sticker_count", return_value=3), \
             patch("mochi.db.get_stickers_by_tag", return_value=[]), \
             patch("mochi.skills.sticker.handler._get_all_stickers", return_value=[sticker]):
            result = await StickerSkill().execute(ctx)
        assert "STICKER:RAND1" in result.output

    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        """Unknown tool name returns error."""
        ctx = self._make_context("unknown_sticker_tool")
        result = await StickerSkill().execute(ctx)
        assert not result.success
        assert "Unknown" in result.output


class TestStickerSkillLearn:

    @pytest.mark.asyncio
    async def test_learn_success(self):
        """learn_sticker saves to DB and returns success info."""
        skill = StickerSkill()
        with patch("mochi.skills.sticker.handler.generate_sticker_tags", new_callable=AsyncMock, return_value="开心,快乐"), \
             patch("mochi.db.save_sticker", return_value=42), \
             patch("mochi.db.get_sticker_count", return_value=10):
            result = await skill.learn_sticker(
                user_id=1, file_id="FILE1", set_name="set", emoji="😊", caption="test"
            )
        assert result["learned"] is True
        assert result["tags"] == "开心,快乐"
        assert result["count"] == 10

    @pytest.mark.asyncio
    async def test_learn_duplicate_returns_not_learned(self):
        """learn_sticker when DB returns None (duplicate) sets learned=False."""
        skill = StickerSkill()
        with patch("mochi.skills.sticker.handler.generate_sticker_tags", new_callable=AsyncMock, return_value="tag"), \
             patch("mochi.db.save_sticker", return_value=None), \
             patch("mochi.db.get_sticker_count", return_value=5):
            result = await skill.learn_sticker(
                user_id=1, file_id="DUP", set_name="set", emoji="😊"
            )
        assert result["learned"] is False


class TestDeleteLastSticker:

    @pytest.mark.asyncio
    async def test_delete_last_success(self):
        """delete_last_sticker removes the recorded sticker."""
        record_last_sent_sticker(100, "FILE_TO_DELETE")
        ctx = SkillContext(
            trigger="tool_call", user_id=1, channel_id=100,
            tool_name="delete_last_sticker",
        )
        with patch("mochi.db.delete_sticker", return_value=True):
            result = await StickerSkill().execute(ctx)
        assert result.success
        assert "删除" in result.output

    @pytest.mark.asyncio
    async def test_delete_no_last_sticker(self):
        """delete_last_sticker when nothing was sent returns error."""
        ctx = SkillContext(
            trigger="tool_call", user_id=1, channel_id=999,
            tool_name="delete_last_sticker",
        )
        result = await StickerSkill().execute(ctx)
        assert "没有找到" in result.output
