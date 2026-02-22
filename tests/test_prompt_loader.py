"""Tests for prompt_loader — hot-reload and prompt retrieval."""

import pytest
from mochi.prompt_loader import get_prompt


class TestGetPrompt:
    def test_loads_existing_prompt(self):
        content = get_prompt("think_system")
        assert content
        assert "Actions" in content or "nothing" in content

    def test_loads_subdirectory_prompt(self):
        content = get_prompt("system_chat/soul")
        assert content
        assert "companion" in content.lower()

    def test_missing_prompt_returns_empty(self):
        assert get_prompt("nonexistent_prompt_xyz") == ""

    def test_memory_extract_loads(self):
        content = get_prompt("memory_extract")
        assert content
