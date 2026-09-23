"""Local history search never performs recall embedding or Diary rollover."""

import pytest

import mochi.db as db
from mochi.diary import diary
from mochi.skills.base import SkillContext
from mochi.skills.internal_search.handler import InternalSearchSkill
from mochi.skills.internal_search import queries


def _context(args, *, turn_id="current"):
    return SkillContext(
        trigger="tool_call", actor="main", source="chat", user_id=1,
        turn_id=turn_id, tool_name="search_personal_history", args=args,
    )


def _page(day, journal, status=""):
    return f"# Diary {day}\n\n## 今日状態\n{status}\n\n## 今日日記\n{journal}\n"


@pytest.mark.asyncio
async def test_search_uses_local_sources_without_mutating_records(monkeypatch):
    db.save_message(1, "user", "orchid older conversation", turn_id="old")
    db.save_message(1, "assistant", "orchid delivered reply", turn_id="old")
    db.save_message(2, "user", "orchid other owner", turn_id="other")
    db.set_context_reset(1)
    db.save_message(1, "user", "orchid current query", turn_id="current")
    mid = db.save_memory_item(1, "orchid remembered")
    db.save_memory_item(2, "orchid private memory")
    diary.path.write_text(
        _page("2026-09-22", "orchid current journal", "orchid hidden status"),
        encoding="utf-8",
    )
    archive = diary.path.parent / "diary_archive"
    archive.mkdir()
    archive_path = archive / "2026-09.md"
    archive_path.write_text(
        _page("2026-09-21", "orchid old journal"),
        encoding="utf-8",
    )
    diary.tomorrow_draft_path.write_text("orchid hidden draft", encoding="utf-8")
    snapshots = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (diary.path, archive_path, diary.tomorrow_draft_path)
    }

    def unexpected_rollover():
        pytest.fail("history search must not roll the Diary")

    monkeypatch.setattr(diary, "_ensure_today", unexpected_rollover)
    result = await InternalSearchSkill().execute(_context({"query": "orchid"}))

    assert result.success and not result.state_changed
    assert result.content_source == "local_history"
    assert result.exposed_memory_ids == [mid]
    assert "Conversation (2)" in result.output and "Diary (2)" in result.output
    assert "Memory (1)" in result.output and "record updated " in result.output
    assert "older conversation" in result.output
    for excluded in ("current query", "other owner", "private memory", "hidden status", "hidden draft"):
        assert excluded not in result.output
    for path, before in snapshots.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    with db._connect() as conn:
        assert conn.execute("SELECT access_count FROM memory_items WHERE id=?", (mid,)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_literal_conversation_query_does_not_expand_sql_wildcards():
    db.save_message(1, "user", r"saved 50%_done\okay", turn_id="old")
    db.save_message(1, "user", "saved 50xxxAdoneZokay", turn_id="other")
    result = await InternalSearchSkill().execute(_context({
        "query": r"50%_done\okay", "source": "conversation",
    }))
    assert result.success and "Conversation (1)" in result.output
    assert "50xxx" not in result.output


@pytest.mark.asyncio
async def test_search_bounds_and_diary_scan_notice(monkeypatch):
    diary.path.write_text(
        _page("2026-09-22", "prefix " * 80 + "orchid " + "suffix " * 80),
        encoding="utf-8",
    )
    archive = diary.path.parent / "diary_archive"
    archive.mkdir()
    for month in ("2026-07", "2026-08"):
        (archive / f"{month}.md").write_text(
            _page(f"{month}-01", "orchid archived"), encoding="utf-8",
        )
    monkeypatch.setattr(queries, "MAX_ARCHIVE_FILES", 1)
    result = await InternalSearchSkill().execute(_context({
        "query": "orchid", "source": "diary", "limit": 1,
    }))
    assert result.success and "Diary (1)" in result.output
    excerpt = next(line for line in result.output.splitlines() if line.startswith("- "))
    assert len(excerpt.split(": ", 1)[1]) <= 282
    assert "…" in excerpt and "local safety limit" in result.output
    monkeypatch.setattr(queries, "MAX_SCAN_BYTES", 10)
    blocked = await InternalSearchSkill().execute(_context({
        "query": "orchid", "source": "diary",
    }))
    assert blocked.success and "No local matches" in blocked.output
    assert "some saved entries were not checked" in blocked.output


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"query": " "}, {"query": "x" * 201}, {"query": "x", "limit": True},
    {"query": "x", "limit": 1.5}, {"query": "x", "limit": 11},
    {"query": "x", "source": "internet"},
])
async def test_search_rejects_invalid_arguments(args):
    result = await InternalSearchSkill().execute(_context(args))
    assert not result.success and result.error_code == "invalid_arguments"


@pytest.mark.asyncio
async def test_search_missing_turn_and_read_failure_are_not_empty_success(monkeypatch):
    skill = InternalSearchSkill()
    missing = await skill.execute(_context({"query": "x"}, turn_id=""))
    assert not missing.success and missing.error_code == "search_context_unavailable"

    def fail(*args, **kwargs):
        raise OSError("private-path-detail")

    monkeypatch.setattr(queries.diary, "_archive_blocks", fail)
    diary.path.write_text("saved diary", encoding="utf-8")
    failed = await skill.execute(_context({"query": "x", "source": "diary"}))
    assert not failed.success and failed.error_code == "local_search_failed"
    assert "private-path-detail" not in failed.output
