"""Diary operations for the workspace skill."""

from datetime import datetime, timezone

import pytest

import mochi.diary as diary_module
from mochi.diary import DailyFile
from mochi.skills.base import SkillContext


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from mochi.skills.workspace.handler import WorkspaceSkill
    import mochi.skills.workspace.handler as workspace_module

    diary = DailyFile(
        path=tmp_path / "diary.md",
        label="Diary",
        max_lines=50,
        sections=("今日状態", "今日日記"),
        section_max_lines={"今日状態": 20, "今日日記": 30},
    )
    monkeypatch.setattr(diary_module, "TZ", timezone.utc)
    monkeypatch.setattr(
        diary_module, "_diary_date",
        lambda: datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(diary_module, "_today_str", lambda: "2025-06-15")
    monkeypatch.setattr(diary_module, "_now_time", lambda: "10:00")
    monkeypatch.setattr(workspace_module, "diary", diary)
    skill = WorkspaceSkill()
    skill._name = "workspace"
    return skill, diary


def _context(tool_name, args):
    return SkillContext(
        trigger="tool_call", user_id=1, tool_name=tool_name, args=args
    )


@pytest.mark.asyncio
async def test_diary_write_can_be_read_back(workspace):
    skill, diary = workspace

    content = "Went to the gym.\n\n## Notes\n\n" + "Full journal line.\n" * 40
    write = await skill.execute(_context("write_diary", {
        "content": content, "_expected_content": "",
        "_source_date": "2025-06-15", "_target_date": "2025-06-15",
    }))
    diary.rewrite_section("今日状態", ["Habit status"])
    read = await skill.execute(_context("read_diary", {}))

    assert write.success
    assert content.strip() in read.output
    assert diary.read("今日日記") == content.strip()
    rejected = await skill.execute(_context("write_diary", {
        "content": "stale edit", "_expected_content": "",
        "_source_date": "2025-06-15", "_target_date": "2025-06-15",
    }))
    assert not rejected.success
    assert rejected.document_snapshot == content.strip()
    assert diary.read("今日日記") == content.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_day", ["2025-06-16", "2025-06-18"])
async def test_tomorrow_journal_reaches_its_exact_date(workspace, monkeypatch, next_day):
    skill, diary = workspace
    diary.replace_section_exact(
        "今日日記", expected_content="", content="Keep today's journal",
        target_date="2025-06-15",
    )
    result = await skill.execute(_context("write_diary", {
        "content": "Carry this forward", "day": "tomorrow", "_expected_content": "",
        "_source_date": "2025-06-15", "_target_date": "2025-06-16",
    }))
    assert result.success
    assert diary.read("今日日記") == "Keep today's journal"
    monkeypatch.setattr(diary_module, "_today_str", lambda: next_day)
    current = diary.read("今日日記")
    archive = (diary.path.parent / "diary_archive" / "2025-06.md").read_text(encoding="utf-8")
    assert "Keep today's journal" in archive
    if next_day == "2025-06-16":
        assert current == "Carry this forward"
    else:
        assert current == ""
        assert "# Diary 2025-06-16" in archive
        assert "Carry this forward" in archive
    assert not diary.tomorrow_draft_path.exists()
