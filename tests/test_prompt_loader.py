"""Tests for prompt_loader — hot-reload and prompt retrieval."""

from pathlib import Path

import pytest
from mochi.prompt_loader import get_prompt, _DATA_PROMPTS_DIR


class TestGetPrompt:
    def test_loads_existing_prompt(self):
        content = get_prompt("think_system")
        assert content
        assert "Actions" in content or "nothing" in content

    def test_loads_subdirectory_prompt(self):
        content = get_prompt("system_chat/soul")
        assert content
        assert "Identity" in content or "陪伴" in content

    def test_missing_prompt_returns_empty(self):
        assert get_prompt("nonexistent_prompt_xyz") == ""

    def test_memory_extract_loads(self):
        content = get_prompt("memory_extract")
        assert content

    def test_user_override_takes_priority(self, tmp_path, monkeypatch):
        """data/prompts/system_chat/soul.md should override prompts/system_chat/soul.md."""
        override_dir = tmp_path / "system_chat"
        override_dir.mkdir(parents=True)
        (override_dir / "soul.md").write_text("custom soul", encoding="utf-8")

        import mochi.prompt_loader as pl
        monkeypatch.setattr(pl, "_DATA_PROMPTS_DIR", tmp_path)

        content = get_prompt("system_chat/soul")
        assert content == "custom soul"

    def test_fallback_when_no_override(self):
        """Without a user override file, default prompt is returned."""
        content = get_prompt("system_chat/soul")
        assert content
        assert "Identity" in content or "陪伴" in content
