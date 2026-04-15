"""Tests for the AI client — system prompt building, sticker regex, chat_proactive, bedtime tidy."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from mochi.ai_client import (
    _build_system_prompt, STICKER_RE, chat_proactive, chat,
    ChatResult, _expand_history, chat_bedtime_tidy,
)
from mochi.llm import LLMResponse
from mochi.transport import IncomingMessage


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
        assert "当前时间" in prompt

    def test_includes_core_memory(self):
        """System prompt includes core memory when provided."""
        with patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(user_id=1, core_memory="User likes cats")
        assert "User likes cats" in prompt
        assert "你对用户的了解" in prompt

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
        assert "工具使用规则" in prompt

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
        assert "当前时间" in prompt


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


def _make_msg(text="hello"):
    return IncomingMessage(user_id=1, channel_id=100, text=text, transport="telegram")


def _ok_response(content="Hi there!"):
    return LLMResponse(
        content=content,
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        model="test-model",
    )


# Shared patch targets to isolate chat() from DB / skills / prompts
_CHAT_PATCHES = {
    "mochi.ai_client.save_message": MagicMock(),
    "mochi.ai_client.get_core_memory": MagicMock(return_value=""),
    "mochi.ai_client.get_recent_messages": MagicMock(return_value=[]),
    "mochi.ai_client.get_prompt": MagicMock(return_value="be nice"),
    "mochi.ai_client.list_habits": MagicMock(return_value=[]),
    "mochi.ai_client.log_usage": MagicMock(),
    "mochi.ai_client.skill_registry.get_tools": MagicMock(return_value=[]),
    "mochi.ai_client.skill_registry.get_skill": MagicMock(return_value=None),
}


def _apply_chat_patches(extra=None):
    """Stack context-manager patches for chat() isolation."""
    import contextlib
    targets = dict(_CHAT_PATCHES)
    if extra:
        targets.update(extra)
    return contextlib.ExitStack(), targets


class TestChatRetry:

    @pytest.mark.asyncio
    async def test_retry_success_on_second_attempt(self):
        """First LLM call fails, retry succeeds — user gets normal reply."""
        mock_client = MagicMock()
        mock_client.chat.side_effect = [
            Exception("Connection timeout"),
            _ok_response("Retry worked!"),
        ]
        targets = dict(_CHAT_PATCHES)
        targets["mochi.ai_client.get_client_for_tier"] = MagicMock(return_value=mock_client)

        import contextlib
        with contextlib.ExitStack() as stack:
            for target, mock_obj in targets.items():
                stack.enter_context(patch(target, mock_obj))
            result = await chat(_make_msg())

        assert result.text == "Retry worked!"
        assert mock_client.chat.call_count == 2

    @pytest.mark.asyncio
    async def test_both_attempts_fail_returns_error(self):
        """Both LLM calls fail — user gets API error message."""
        mock_client = MagicMock()
        mock_client.chat.side_effect = Exception("Insufficient quota")
        targets = dict(_CHAT_PATCHES)
        targets["mochi.ai_client.get_client_for_tier"] = MagicMock(return_value=mock_client)

        import contextlib
        with contextlib.ExitStack() as stack:
            for target, mock_obj in targets.items():
                stack.enter_context(patch(target, mock_obj))
            result = await chat(_make_msg())

        assert "API 报错" in result.text
        assert "Insufficient quota" in result.text
        assert mock_client.chat.call_count == 2

    @pytest.mark.asyncio
    async def test_first_attempt_success_no_retry(self):
        """LLM call succeeds on first try — no retry needed."""
        mock_client = MagicMock()
        mock_client.chat.return_value = _ok_response("All good!")
        targets = dict(_CHAT_PATCHES)
        targets["mochi.ai_client.get_client_for_tier"] = MagicMock(return_value=mock_client)

        import contextlib
        with contextlib.ExitStack() as stack:
            for target, mock_obj in targets.items():
                stack.enter_context(patch(target, mock_obj))
            result = await chat(_make_msg())

        assert result.text == "All good!"
        assert mock_client.chat.call_count == 1


class TestExpandHistory:

    def test_no_tools(self):
        """Messages without tool_history pass through unchanged."""
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there", "tool_history": None},
        ]
        result = _expand_history(history)
        assert len(result) == 2
        assert result[0] == {"role": "user", "content": "hello"}
        assert result[1] == {"role": "assistant", "content": "hi there"}

    def test_with_tools(self):
        """tool_history expands into 3-message API-native sequence."""
        history = [
            {"role": "user", "content": "what's the weather?"},
            {
                "role": "assistant",
                "content": "It's sunny in Tokyo!",
                "tool_history": '[{"name": "check_weather"}]',
            },
        ]
        result = _expand_history(history)
        assert len(result) == 4  # user + assistant(tool_calls) + tool(result) + assistant(text)

        # 1. User message unchanged
        assert result[0] == {"role": "user", "content": "what's the weather?"}

        # 2. Assistant with tool_calls (content=None)
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] is None
        assert len(result[1]["tool_calls"]) == 1
        tc = result[1]["tool_calls"][0]
        assert tc["function"]["name"] == "check_weather"
        assert tc["id"] == "hist_1_0"

        # 3. Tool result
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "hist_1_0"
        assert result[2]["content"] == "OK"

        # 4. Assistant with original reply
        assert result[3] == {"role": "assistant", "content": "It's sunny in Tokyo!"}

    def test_multiple_tools(self):
        """Multiple tools in one turn expand correctly."""
        history = [
            {
                "role": "assistant",
                "content": "Here's what I found.",
                "tool_history": '[{"name": "web_search"}, {"name": "check_weather"}]',
            },
        ]
        result = _expand_history(history)
        # assistant(tool_calls) + tool(web_search) + tool(check_weather) + assistant(text)
        assert len(result) == 4
        assert len(result[0]["tool_calls"]) == 2
        assert result[1]["tool_call_id"] == "hist_0_0"
        assert result[2]["tool_call_id"] == "hist_0_1"

    def test_invalid_json_fallback(self):
        """Invalid JSON in tool_history falls back to plain message."""
        history = [
            {"role": "assistant", "content": "broken", "tool_history": "not valid json"},
        ]
        result = _expand_history(history)
        assert len(result) == 1
        assert result[0] == {"role": "assistant", "content": "broken"}

    def test_empty_array(self):
        """Empty array tool_history does not expand."""
        history = [
            {"role": "assistant", "content": "no tools", "tool_history": "[]"},
        ]
        result = _expand_history(history)
        assert len(result) == 1
        assert result[0] == {"role": "assistant", "content": "no tools"}

    def test_empty_string(self):
        """Empty string tool_history does not expand."""
        history = [
            {"role": "assistant", "content": "no tools", "tool_history": ""},
        ]
        result = _expand_history(history)
        assert len(result) == 1
        assert result[0] == {"role": "assistant", "content": "no tools"}

    def test_missing_tool_history_key(self):
        """Messages without tool_history key at all work fine."""
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = _expand_history(history)
        assert len(result) == 2


class TestBedtimeTidyToolResolution:
    """Bedtime tidy must resolve tools correctly and invoke them."""

    def test_bedtime_tidy_tools_config_resolves(self):
        """BEDTIME_TIDY_TOOLS config matches registered skill names."""
        from mochi.config import BEDTIME_TIDY_TOOLS
        import mochi.skills as skill_registry

        tools = skill_registry.get_tools_by_names(BEDTIME_TIDY_TOOLS)
        tool_names = [t["function"]["name"] for t in tools]
        assert "manage_note" in tool_names, (
            f"manage_note not found in resolved tools. "
            f"Config={BEDTIME_TIDY_TOOLS}, got={tool_names}"
        )
        assert "manage_todo" in tool_names, (
            f"manage_todo not found in resolved tools. "
            f"Config={BEDTIME_TIDY_TOOLS}, got={tool_names}"
        )

    @pytest.mark.asyncio
    async def test_bedtime_tidy_invokes_tool(self, tmp_path, monkeypatch):
        """Bedtime tidy LLM tool call reaches the skill dispatch layer."""
        import mochi.skills.note.handler as note_mod
        monkeypatch.setattr(note_mod, "_NOTES_PATH", tmp_path / "notes.md")

        # Seed a short-term note
        note_skill = note_mod.NoteSkill()
        from mochi.skills.base import SkillContext
        await note_skill.execute(SkillContext(
            trigger="tool_call", user_id=1,
            tool_name="manage_note",
            args={"action": "add", "content": "today only note"},
        ))

        # LLM response: first call returns a tool_call to remove note #1,
        # second call returns final text
        response_with_tool = LLMResponse(
            content="",
            prompt_tokens=100, completion_tokens=20, total_tokens=120,
            model="test-model",
            tool_calls=[{
                "id": "tc_1",
                "name": "manage_note",
                "arguments": {"action": "remove", "note_id": 1},
            }],
        )
        response_final = LLMResponse(
            content="Good night!",
            prompt_tokens=100, completion_tokens=10, total_tokens=110,
            model="test-model",
        )
        mock_client = MagicMock()
        mock_client.chat.side_effect = [response_with_tool, response_final]

        import mochi.ai_client as ai_mod
        monkeypatch.setattr(ai_mod, "_last_bedtime_tidy_date", "")

        with patch("mochi.ai_client.get_client_for_tier", return_value=mock_client), \
             patch("mochi.ai_client.get_prompt", return_value="Tidy: {findings_text}"), \
             patch("mochi.ai_client.get_core_memory", return_value=""), \
             patch("mochi.ai_client.get_recent_messages", return_value=[]), \
             patch("mochi.ai_client.log_usage"):
            result = await chat_bedtime_tidy(
                [{"topic": "sleep_transition", "summary": "goodnight"}],
                user_id=1,
            )

        assert result == "Good night!"

        # Verify the note was actually removed via dispatch
        remaining = note_mod._read_notes()
        assert len(remaining) == 0, f"Expected note removed, but got: {remaining}"

    @pytest.mark.asyncio
    async def test_bedtime_tidy_no_variable_shadowing(self):
        """Regression: skill_registry must not be shadowed inside chat_bedtime_tidy."""
        mock_client = MagicMock()
        mock_client.chat.return_value = LLMResponse(
            content="Goodnight!",
            prompt_tokens=50, completion_tokens=10, total_tokens=60,
            model="test-model",
        )

        import mochi.ai_client as ai_mod
        ai_mod._last_bedtime_tidy_date = ""

        with patch("mochi.ai_client.get_client_for_tier", return_value=mock_client), \
             patch("mochi.ai_client.get_prompt", return_value="Tidy: {findings_text}"), \
             patch("mochi.ai_client.get_core_memory", return_value=""), \
             patch("mochi.ai_client.get_recent_messages", return_value=[]), \
             patch("mochi.ai_client.log_usage"):
            # Should NOT raise UnboundLocalError
            result = await chat_bedtime_tidy(
                [{"topic": "test", "summary": "test"}],
                user_id=1,
            )
        assert result is not None
