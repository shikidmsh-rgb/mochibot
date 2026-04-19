"""Tests for conversation summary feature (caching, bucketing, generation)."""

import asyncio
import pytest
from unittest.mock import patch, MagicMock

from mochi.db import (
    init_db,
    save_message,
    get_recent_messages,
    get_cached_summary,
    save_cached_summary,
    cleanup_summary_cache,
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Ensure a fresh database for each test."""
    db_path = tmp_path / "test.db"
    import mochi.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    init_db()
    yield db_path


class TestSummaryCacheDB:
    """Tests for L2 DB cache functions."""

    def test_save_and_get(self):
        save_cached_summary(1, 16, "我们之前聊了天气和心情")
        result = get_cached_summary(1, 16)
        assert result == "我们之前聊了天气和心情"

    def test_get_miss(self):
        assert get_cached_summary(1, 16) is None

    def test_upsert(self):
        save_cached_summary(1, 16, "old summary")
        save_cached_summary(1, 16, "new summary")
        assert get_cached_summary(1, 16) == "new summary"

    def test_different_buckets(self):
        save_cached_summary(1, 16, "bucket 16")
        save_cached_summary(1, 32, "bucket 32")
        assert get_cached_summary(1, 16) == "bucket 16"
        assert get_cached_summary(1, 32) == "bucket 32"

    def test_different_users(self):
        save_cached_summary(1, 16, "user 1")
        save_cached_summary(2, 16, "user 2")
        assert get_cached_summary(1, 16) == "user 1"
        assert get_cached_summary(2, 16) == "user 2"

    def test_cleanup(self):
        save_cached_summary(1, 16, "old")
        # Cleanup with 0 days retains nothing
        deleted = cleanup_summary_cache(retain_days=0)
        assert deleted == 1
        assert get_cached_summary(1, 16) is None


class TestConvSummaryLogic:
    """Tests for the summary generation logic in ai_client."""

    def _populate_messages(self, user_id: int, count: int):
        """Insert count messages alternating user/assistant."""
        for i in range(count):
            role = "user" if i % 2 == 0 else "assistant"
            save_message(user_id, role, f"message {i}")

    def test_no_summary_when_short(self):
        """Conv summary should return None when history fits in window."""
        from mochi.ai_client import _get_conv_summary
        self._populate_messages(1, 10)  # well under threshold
        result = asyncio.get_event_loop().run_until_complete(_get_conv_summary(1))
        assert result is None

    def test_no_summary_at_threshold(self):
        """Exactly at threshold (20 messages) should not trigger summary."""
        from mochi.ai_client import _get_conv_summary
        self._populate_messages(1, 20)
        result = asyncio.get_event_loop().run_until_complete(_get_conv_summary(1))
        assert result is None

    @patch("mochi.ai_client.get_client_for_tier")
    def test_summary_generated_over_threshold(self, mock_get_client):
        """Over threshold should attempt to generate summary."""
        from mochi.ai_client import _get_conv_summary, _conv_summary_cache

        # Clear cache
        _conv_summary_cache.clear()

        mock_response = MagicMock()
        mock_response.content = "我们之前聊了各种日常话题"
        mock_response.prompt_tokens = 100
        mock_response.completion_tokens = 30
        mock_response.total_tokens = 130
        mock_response.reasoning_tokens = None
        mock_response.cached_prompt_tokens = None
        mock_client = MagicMock()
        mock_client.chat.return_value = mock_response
        mock_get_client.return_value = mock_client

        self._populate_messages(1, 22)  # over threshold
        result = asyncio.get_event_loop().run_until_complete(_get_conv_summary(1))
        assert result == "我们之前聊了各种日常话题"
        assert mock_client.chat.called

    def test_l1_cache_hit(self):
        """Repeated calls should hit L1 memory cache."""
        from mochi.ai_client import _get_conv_summary, _conv_summary_cache

        self._populate_messages(1, 22)
        # Pre-populate L1 cache
        # bucket = (22 // 16) * 16 = 16
        _conv_summary_cache[(1, 16)] = "cached summary"

        result = asyncio.get_event_loop().run_until_complete(_get_conv_summary(1))
        assert result == "cached summary"

        # Cleanup
        _conv_summary_cache.clear()

    def test_l2_cache_hit(self):
        """Should load from DB if L1 misses."""
        from mochi.ai_client import _get_conv_summary, _conv_summary_cache

        _conv_summary_cache.clear()
        self._populate_messages(1, 22)
        # Pre-populate L2 cache
        save_cached_summary(1, 16, "db cached summary")

        result = asyncio.get_event_loop().run_until_complete(_get_conv_summary(1))
        assert result == "db cached summary"

        # Cleanup
        _conv_summary_cache.clear()

    def test_bucket_calculation(self):
        """Verify bucketing: 22 msgs -> bucket 16, 33 msgs -> bucket 32."""
        from mochi.config import CONV_SUMMARY_BUCKET_SIZE
        assert (22 // CONV_SUMMARY_BUCKET_SIZE) * CONV_SUMMARY_BUCKET_SIZE == 16
        assert (33 // CONV_SUMMARY_BUCKET_SIZE) * CONV_SUMMARY_BUCKET_SIZE == 32


class TestPrewarm:
    """Tests for pre-warm boundary detection."""

    def _populate_messages(self, user_id: int, count: int):
        for i in range(count):
            role = "user" if i % 2 == 0 else "assistant"
            save_message(user_id, role, f"message {i}")

    @patch("mochi.ai_client._prewarm_conv_summary")
    def test_no_prewarm_below_boundary(self, mock_prewarm):
        """Should not trigger prewarm when not near bucket boundary."""
        from mochi.ai_client import prewarm_conv_summary_if_needed

        self._populate_messages(1, 10)

        # Need an event loop for create_task
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(asyncio.sleep(0))  # ensure loop is running context
            prewarm_conv_summary_if_needed(1)
        finally:
            loop.close()

        mock_prewarm.assert_not_called()


class TestBuildSystemPrompt:
    """Tests for conv_summary injection into system prompt."""

    @patch("mochi.ai_client.get_system_chat_modules")
    @patch("mochi.ai_client.get_prompt")
    def test_conv_summary_injected(self, mock_get_prompt, mock_modules):
        from mochi.ai_client import _build_system_prompt

        mock_modules.return_value = {"soul": "I am a bot."}
        mock_get_prompt.return_value = ""

        result = _build_system_prompt(
            user_id=1,
            conv_summary="我们之前聊了天气",
        )
        assert "本次对话早期内容（摘要）" in result
        assert "我们之前聊了天气" in result

    @patch("mochi.ai_client.get_system_chat_modules")
    @patch("mochi.ai_client.get_prompt")
    def test_no_injection_when_empty(self, mock_get_prompt, mock_modules):
        from mochi.ai_client import _build_system_prompt

        mock_modules.return_value = {"soul": "I am a bot."}
        mock_get_prompt.return_value = ""

        result = _build_system_prompt(user_id=1, conv_summary="")
        assert "本次对话早期内容" not in result
