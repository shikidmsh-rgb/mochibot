"""Tests for prompt_loader — personality section extraction and full prompt assembly."""

import pytest
from mochi.prompt_loader import _extract_section, get_personality, get_full_prompt, get_prompt


class TestExtractSection:
    def test_extract_chat(self):
        text = "## Chat\nYou are warm.\n\n## Think\nYou analyze."
        assert _extract_section(text, "Chat") == "You are warm."

    def test_extract_think(self):
        text = "## Chat\nYou are warm.\n\n## Think\nYou analyze.\nBe conservative."
        result = _extract_section(text, "Think")
        assert "You analyze." in result
        assert "Be conservative." in result

    def test_missing_section(self):
        text = "## Chat\nHello"
        assert _extract_section(text, "Think") == ""

    def test_empty_text(self):
        assert _extract_section("", "Chat") == ""


class TestGetPersonality:
    def test_chat_section(self):
        chat = get_personality("Chat")
        assert "Mochi" in chat
        assert "warm" in chat.lower() or "friendly" in chat.lower()

    def test_think_section(self):
        think = get_personality("Think")
        assert "background" in think.lower() or "observe" in think.lower()

    def test_nonexistent_section(self):
        assert get_personality("Nonexistent") == ""


class TestGetFullPrompt:
    def test_chat_prompt_has_personality(self):
        full = get_full_prompt("system_chat", "Chat")
        # Should have personality (Mochi) AND task rules (Core Rules)
        assert "Mochi" in full
        assert "Core Rules" in full

    def test_think_prompt_has_personality(self):
        full = get_full_prompt("think_system", "Think")
        # Should have Think personality AND task logic (Actions)
        assert "background" in full.lower() or "observe" in full.lower()
        assert "Actions" in full or "nothing" in full

    def test_personality_not_in_memory_extract(self):
        full = get_full_prompt("memory_extract")
        # Functional prompt — no personality injected
        assert "Mochi" not in full

    def test_separator_present(self):
        full = get_full_prompt("system_chat", "Chat")
        assert "---" in full

    def test_report_gets_chat_personality(self):
        full = get_full_prompt("report_morning", "Chat")
        assert "Mochi" in full
        assert "morning" in full.lower() or "briefing" in full.lower()
