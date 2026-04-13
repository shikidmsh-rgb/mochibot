"""Tests for mochi/skills/note/handler.py — NoteSkill and helpers."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from mochi.skills.base import SkillContext, SkillResult
from mochi.skills.note.handler import NoteSkill, read_notes_for_observation


def _make_ctx(action: str, **kwargs) -> SkillContext:
    args = {"action": action, **kwargs}
    return SkillContext(trigger="tool_call", user_id=1, tool_name="manage_notes", args=args)


class TestNoteSkillAdd:

    @pytest.mark.asyncio
    async def test_add_success(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        result = await skill.execute(_make_ctx("add", content="Buy milk"))
        assert result.success is True
        assert "note added" in result.output
        assert "1 total" in result.output

    @pytest.mark.asyncio
    async def test_add_appends_date_tag(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        await skill.execute(_make_ctx("add", content="Test note"))
        content = (tmp_path / "notes.md").read_text(encoding="utf-8")
        # Should contain a date tag like (2026-04-13)
        assert "(" in content and ")" in content

    @pytest.mark.asyncio
    async def test_add_preserves_existing_date_tag(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        result = await skill.execute(_make_ctx("add", content="Meeting (2025-01-01)"))
        assert result.success is True
        content = (tmp_path / "notes.md").read_text(encoding="utf-8")
        assert "2025-01-01" in content

    @pytest.mark.asyncio
    async def test_add_empty_content_error(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        result = await skill.execute(_make_ctx("add", content=""))
        assert "content is required" in result.output


class TestNoteSkillList:

    @pytest.mark.asyncio
    async def test_list_empty(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        result = await skill.execute(_make_ctx("list"))
        assert result.output == "No notes."

    @pytest.mark.asyncio
    async def test_list_numbered(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        await skill.execute(_make_ctx("add", content="First"))
        await skill.execute(_make_ctx("add", content="Second"))
        result = await skill.execute(_make_ctx("list"))
        assert "1." in result.output
        assert "2." in result.output


class TestNoteSkillRemove:

    @pytest.mark.asyncio
    async def test_remove_success(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        await skill.execute(_make_ctx("add", content="To remove"))
        result = await skill.execute(_make_ctx("remove", note_id=1))
        assert result.success is True
        assert "removed" in result.output.lower()

    @pytest.mark.asyncio
    async def test_remove_invalid_id(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        result = await skill.execute(_make_ctx("remove", note_id="abc"))
        assert "must be a number" in result.output

    @pytest.mark.asyncio
    async def test_remove_out_of_range(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        await skill.execute(_make_ctx("add", content="Only note"))
        result = await skill.execute(_make_ctx("remove", note_id=5))
        assert "out of range" in result.output

    @pytest.mark.asyncio
    async def test_remove_missing_note_id(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        result = await skill.execute(_make_ctx("remove"))
        assert "note_id is required" in result.output


class TestNoteSkillUnknown:

    @pytest.mark.asyncio
    async def test_unknown_action(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        skill = NoteSkill()
        result = await skill.execute(_make_ctx("dance"))
        assert result.success is False
        assert "Unknown action" in result.output


class TestReadNotesForObservation:

    def test_empty_when_no_file(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        monkeypatch.setattr(mod, "_NOTES_PATH", tmp_path / "notes.md")
        assert read_notes_for_observation() == ""

    def test_empty_when_only_header(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        path = tmp_path / "notes.md"
        path.write_text("# Notes", encoding="utf-8")
        monkeypatch.setattr(mod, "_NOTES_PATH", path)
        assert read_notes_for_observation() == ""

    def test_returns_content(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        path = tmp_path / "notes.md"
        path.write_text("# Notes\n\n## Notes\n- Buy milk\n- Fix bug\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_NOTES_PATH", path)
        result = read_notes_for_observation()
        assert "## Notes" in result
        assert "Buy milk" in result

    def test_compact_truncates(self, tmp_path, monkeypatch):
        import mochi.skills.note.handler as mod
        path = tmp_path / "notes.md"
        long_content = "# Notes\n\n" + "- " + "x" * 400 + "\n"
        path.write_text(long_content, encoding="utf-8")
        monkeypatch.setattr(mod, "_NOTES_PATH", path)
        result = read_notes_for_observation(compact=True)
        assert "..." in result
