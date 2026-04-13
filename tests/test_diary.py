"""Tests for mochi/diary.py — DailyFile ops, sections, archive, refresh."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from pathlib import Path

import mochi.diary as diary_mod
from mochi.diary import DailyFile


UTC = timezone.utc


@pytest.fixture
def daily_file(tmp_path, monkeypatch):
    """Create a DailyFile with tmp_path and fixed date."""
    monkeypatch.setattr(diary_mod, "TZ", UTC)
    monkeypatch.setattr(diary_mod, "_diary_date",
                        lambda: datetime(2025, 6, 15, 10, 0, tzinfo=UTC))
    monkeypatch.setattr(diary_mod, "_today_str", lambda: "2025-06-15")
    monkeypatch.setattr(diary_mod, "_now_time", lambda: "10:00")

    return DailyFile(
        path=tmp_path / "diary.md",
        label="TestDiary",
        max_lines=20,
    )


@pytest.fixture
def sectioned_file(tmp_path, monkeypatch):
    """Create a DailyFile with sections."""
    monkeypatch.setattr(diary_mod, "TZ", UTC)
    monkeypatch.setattr(diary_mod, "_diary_date",
                        lambda: datetime(2025, 6, 15, 10, 0, tzinfo=UTC))
    monkeypatch.setattr(diary_mod, "_today_str", lambda: "2025-06-15")
    monkeypatch.setattr(diary_mod, "_now_time", lambda: "10:00")

    return DailyFile(
        path=tmp_path / "diary.md",
        label="TestDiary",
        max_lines=50,
        sections=("Status", "Notes"),
        section_max_lines={"Status": 10, "Notes": 40},
    )


# ── Append ──

class TestAppend:

    def test_basic_append(self, daily_file):
        result = daily_file.append("hello world")
        assert "Recorded" in result
        content = daily_file.read()
        assert "hello world" in content

    def test_empty_rejected(self, daily_file):
        result = daily_file.append("")
        assert "empty" in result.lower()

    def test_truncation(self, daily_file):
        long_entry = "x" * 200
        daily_file.append(long_entry)
        content = daily_file.read()
        assert "..." in content

    def test_dedup_exact(self, daily_file):
        daily_file.append("same entry")
        result = daily_file.append("same entry")
        assert "Already recorded" in result

    def test_dedup_prefix(self, monkeypatch, tmp_path):
        monkeypatch.setattr(diary_mod, "_diary_date",
                            lambda: datetime(2025, 6, 15, 10, 0, tzinfo=UTC))
        monkeypatch.setattr(diary_mod, "_today_str", lambda: "2025-06-15")
        monkeypatch.setattr(diary_mod, "_now_time", lambda: "10:00")

        df = DailyFile(
            path=tmp_path / "diary.md",
            label="Test",
            max_lines=20,
            topic_dedup_prefixes=("Weather:",),
        )
        df.append("Weather: sunny")
        result = df.append("Weather: rainy")
        assert "Already recorded" in result


# ── Upsert ──

class TestUpsert:

    def test_insert_new(self, daily_file):
        result = daily_file.upsert("mood", "mood: happy")
        assert "Recorded" in result

    def test_replace_existing(self, daily_file):
        daily_file.upsert("mood", "mood: happy")
        result = daily_file.upsert("mood", "mood: sad")
        assert "Replaced" in result
        content = daily_file.read()
        assert "sad" in content
        # old value should be gone
        lines = [l for l in content.split("\n") if "mood" in l.lower()]
        assert len(lines) == 1


# ── Remove ──

class TestRemove:

    def test_existing(self, daily_file):
        daily_file.upsert("mood", "mood: happy")
        result = daily_file.remove("mood")
        assert "Removed" in result
        assert "mood" not in daily_file.read()

    def test_not_found(self, daily_file):
        result = daily_file.remove("nonexistent")
        assert "Not found" in result


# ── Rewrite ──

class TestRewrite:

    def test_basic(self, daily_file):
        result = daily_file.rewrite("- line1\n- line2")
        assert "rewritten" in result.lower()
        content = daily_file.read()
        assert "line1" in content
        assert "line2" in content

    def test_empty_rejected(self, daily_file):
        result = daily_file.rewrite("")
        assert "empty" in result.lower()

    def test_line_limit(self, monkeypatch, tmp_path):
        monkeypatch.setattr(diary_mod, "_diary_date",
                            lambda: datetime(2025, 6, 15, 10, 0, tzinfo=UTC))
        monkeypatch.setattr(diary_mod, "_today_str", lambda: "2025-06-15")
        monkeypatch.setattr(diary_mod, "_now_time", lambda: "10:00")

        df = DailyFile(
            path=tmp_path / "diary.md",
            label="Test",
            max_lines=3,
        )
        lines = "\n".join(f"- line{i}" for i in range(10))
        df.rewrite(lines)
        content = df.read()
        assert content.count("line") <= 3


# ── Sections ──

class TestSections:

    def test_append_to_section(self, sectioned_file):
        sectioned_file.append("test item", section="Status")
        content = sectioned_file.read(section="Status")
        assert "test item" in content

    def test_rewrite_section(self, sectioned_file):
        result = sectioned_file.rewrite_section("Status", ["- item1", "- item2"])
        assert "rewritten" in result.lower()
        content = sectioned_file.read(section="Status")
        assert "item1" in content
        assert "item2" in content

    def test_read_section(self, sectioned_file):
        sectioned_file.append("status entry", section="Status")
        sectioned_file.append("note entry", section="Notes")
        status = sectioned_file.read(section="Status")
        notes = sectioned_file.read(section="Notes")
        assert "status entry" in status
        assert "note entry" in notes
        assert "note entry" not in status

    def test_unknown_section_error(self, sectioned_file):
        result = sectioned_file.rewrite_section("Nonexistent", ["- x"])
        assert "unknown section" in result.lower()


# ── Archive ──

class TestArchive:

    def test_snapshot(self, daily_file, tmp_path):
        daily_file.append("test entry")
        raw = daily_file.read_raw()
        daily_file.snapshot(raw)
        archive_dir = tmp_path / "testdiary_archive"
        assert archive_dir.exists()
        files = list(archive_dir.glob("*.md"))
        assert len(files) == 1

    def test_clear(self, daily_file):
        daily_file.append("test entry")
        daily_file.clear()
        raw = daily_file.read_raw()
        assert raw == ""


# ── refresh_diary_status ──

class TestRefreshDiaryStatus:

    def test_no_user(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "OWNER_USER_ID", 0)
        result = diary_mod.refresh_diary_status(user_id=0)
        assert "No user configured" in result

    def test_with_habits(self, monkeypatch, tmp_path):
        """refresh_diary_status delegates to collect_diary_status and writes to diary."""
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "OWNER_USER_ID", 1)

        # Patch diary instance to use tmp_path
        test_diary = DailyFile(
            path=tmp_path / "diary.md",
            label="Diary",
            max_lines=20,
            sections=("今日状態", "今日日記"),
            section_max_lines={"今日状態": 20, "今日日記": 50},
        )
        monkeypatch.setattr(diary_mod, "diary", test_diary)
        monkeypatch.setattr(diary_mod, "_diary_date",
                            lambda: datetime(2025, 6, 15, 10, 0, tzinfo=UTC))
        monkeypatch.setattr(diary_mod, "_today_str", lambda: "2025-06-15")

        # Mock collect_diary_status to return habit lines
        with patch("mochi.skills.collect_diary_status", return_value=[
            "- Drink Water (0/3) ⏳",
        ]):
            result = diary_mod.refresh_diary_status(user_id=1)

        assert "rewritten" in result.lower()
        content = test_diary.read(section="今日状態")
        assert "Drink Water" in content
