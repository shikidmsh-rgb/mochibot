"""Tests for the AI client — system prompt building, sticker regex, chat_proactive."""

import pytest
from unittest.mock import patch, MagicMock

from mochi.ai_client import _build_system_prompt, STICKER_RE, chat_proactive
from mochi.llm import LLMResponse


class TestBuildSystemPrompt:

    def test_includes_personality(self):
        """System prompt includes personality from soul prompt."""
        with patch("mochi.ai_client.get_prompt") as mock_prompt:
            mock_prompt.side_effect = lambda name: {
                "system_chat/soul": "I am a friendly bot",
                "system_chat/agent": "I help with tasks",
            }.get(name, "")
            prompt = _build_system_prompt(user_id=1)
        assert "friendly bot" in prompt

    def test_includes_time(self):
        """System prompt includes current time section."""
        with patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(user_id=1)
        assert "Current time" in prompt

    def test_includes_core_memory(self):
        """System prompt includes core memory when provided."""
        with patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(user_id=1, core_memory="User likes cats")
        assert "User likes cats" in prompt
        assert "What you know about the user" in prompt

    def test_no_core_memory_section_when_empty(self):
        """Core memory section is omitted when empty."""
        with patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(user_id=1, core_memory="")
        assert "What you know about the user" not in prompt

    def test_includes_usage_rules(self):
        """System prompt includes tool usage rules when provided."""
        with patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(user_id=1, usage_rules="Always be polite")
        assert "Always be polite" in prompt
        assert "Tool usage rules" in prompt

    def test_includes_habits_when_habit_tools_present(self):
        """System prompt includes habit list when habit tools are in tool_names."""
        habits = [
            {"id": 1, "name": "Read", "frequency": "daily:1"},
            {"id": 2, "name": "Exercise", "frequency": "weekly:3"},
        ]
        with patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(
                user_id=1,
                tool_names=["checkin_habit", "query_habit"],
                habits=habits,
            )
        assert "Read" in prompt
        assert "Exercise" in prompt

    def test_no_habits_when_no_habit_tools(self):
        """Habit list is omitted when no habit tools are in tool_names."""
        habits = [{"id": 1, "name": "Read", "frequency": "daily:1"}]
        with patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(
                user_id=1,
                tool_names=["save_memory"],
                habits=habits,
            )
        assert "习惯列表" not in prompt

    def test_fallback_when_no_prompts(self):
        """Returns default fallback if personality prompts are empty/missing."""
        with patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(user_id=1)
        # Should still have at least the time section
        assert "Current time" in prompt


class TestStickerRegex:

    def test_single_marker(self):
        """Extracts single [STICKER:file_id] marker."""
        text = "Here [STICKER:ABC123] go"
        matches = STICKER_RE.findall(text)
        assert matches == ["ABC123"]

    def test_multiple_markers(self):
        """Extracts multiple sticker markers."""
        text = "[STICKER:A1] and [STICKER:B2] and [STICKER:C3]"
        matches = STICKER_RE.findall(text)
        assert matches == ["A1", "B2", "C3"]

    def test_no_markers(self):
        """No markers returns empty list."""
        text = "Just a normal message with no stickers"
        matches = STICKER_RE.findall(text)
        assert matches == []

    def test_marker_removal(self):
        """STICKER_RE.sub removes markers from text."""
        text = "Hello [STICKER:X1] world"
        clean = STICKER_RE.sub("", text).strip()
        assert clean == "Hello  world"


class TestChatProactive:

    @pytest.mark.asyncio
    async def test_success(self):
        """chat_proactive generates a message from findings."""
        mock_client = MagicMock()
        mock_client.chat.return_value = LLMResponse(
            content="Good morning! Hope you slept well.",
            prompt_tokens=100, completion_tokens=20, total_tokens=120,
            model="test-model",
        )
        with patch("mochi.ai_client.get_client_for_tier", return_value=mock_client), \
             patch("mochi.ai_client.get_prompt", return_value="Generate a message based on: {findings_text}"), \
             patch("mochi.ai_client.get_core_memory", return_value="User is a morning person"), \
             patch("mochi.ai_client.get_recent_messages", return_value=[]), \
             patch("mochi.ai_client.log_usage"):
            result = await chat_proactive(
                [{"topic": "morning", "summary": "First tick of the day"}],
                user_id=1,
            )
        assert result is not None
        assert "morning" in result.lower() or "slept" in result.lower()

    @pytest.mark.asyncio
    async def test_skip_sentinel(self):
        """chat_proactive returns [SKIP] when LLM vetoes."""
        mock_client = MagicMock()
        mock_client.chat.return_value = LLMResponse(
            content="[SKIP] Not worth messaging right now.",
            prompt_tokens=50, completion_tokens=10, total_tokens=60,
            model="test-model",
        )
        with patch("mochi.ai_client.get_client_for_tier", return_value=mock_client), \
             patch("mochi.ai_client.get_prompt", return_value="Generate: {findings_text}"), \
             patch("mochi.ai_client.get_core_memory", return_value=""), \
             patch("mochi.ai_client.get_recent_messages", return_value=[]), \
             patch("mochi.ai_client.log_usage"):
            result = await chat_proactive(
                [{"topic": "silence", "summary": "User has been silent"}],
                user_id=1,
            )
        assert result == "[SKIP]"

    @pytest.mark.asyncio
    async def test_empty_findings(self):
        """chat_proactive returns None for empty findings list."""
        result = await chat_proactive([], user_id=1)
        assert result is None

    @pytest.mark.asyncio
    async def test_llm_exception_returns_none(self):
        """chat_proactive returns None on LLM failure."""
        with patch("mochi.ai_client.get_client_for_tier", side_effect=Exception("API down")), \
             patch("mochi.ai_client.get_prompt", return_value="Prompt: {findings_text}"), \
             patch("mochi.ai_client.get_core_memory", return_value=""), \
             patch("mochi.ai_client.get_recent_messages", return_value=[]):
            result = await chat_proactive(
                [{"topic": "test", "summary": "test"}],
                user_id=1,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_llm_response_returns_none(self):
        """chat_proactive returns None when LLM returns empty string."""
        mock_client = MagicMock()
        mock_client.chat.return_value = LLMResponse(
            content="",
            prompt_tokens=50, completion_tokens=0, total_tokens=50,
            model="test-model",
        )
        with patch("mochi.ai_client.get_client_for_tier", return_value=mock_client), \
             patch("mochi.ai_client.get_prompt", return_value="Prompt: {findings_text}"), \
             patch("mochi.ai_client.get_core_memory", return_value=""), \
             patch("mochi.ai_client.get_recent_messages", return_value=[]), \
             patch("mochi.ai_client.log_usage"):
            result = await chat_proactive(
                [{"topic": "test", "summary": "test"}],
                user_id=1,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_prompt_returns_none(self):
        """chat_proactive returns None when proactive_chat prompt is missing."""
        with patch("mochi.ai_client.get_core_memory", return_value=""), \
             patch("mochi.ai_client.get_recent_messages", return_value=[]), \
             patch("mochi.ai_client.get_prompt", return_value=""):
            result = await chat_proactive(
                [{"topic": "test", "summary": "test"}],
                user_id=1,
            )
        assert result is None
