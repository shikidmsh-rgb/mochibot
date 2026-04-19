"""Framework auto-refresh of diary 今日状態 after skill writes.

Verifies Skill.run() triggers refresh_diary_status() iff:
  - execute() succeeded, AND
  - subclass overrode diary_status()
"""

import os
import tempfile
from unittest.mock import patch

import pytest

_temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_temp_db.close()
os.environ["MOCHIBOT_DB_PATH"] = _temp_db.name

from mochi.db import init_db
init_db()

from mochi.skills.base import Skill, SkillContext, SkillResult


class _PlainSkill(Skill):
    name = "plain"
    description = "skill without diary_status override"

    async def execute(self, context: SkillContext) -> SkillResult:
        return SkillResult(output="ok")


class _DiarySkill(Skill):
    name = "with_diary"
    description = "skill that overrides diary_status"

    async def execute(self, context: SkillContext) -> SkillResult:
        return SkillResult(output="ok")

    def diary_status(self, user_id, today, now):
        return ["- contributed line"]


class _FailingDiarySkill(Skill):
    name = "fail_diary"
    description = "skill that overrides diary_status but execute fails"

    async def execute(self, context: SkillContext) -> SkillResult:
        return SkillResult(output="oops", success=False)

    def diary_status(self, user_id, today, now):
        return ["- contributed line"]


class _RaisingDiarySkill(Skill):
    name = "raise_diary"
    description = "skill where execute raises"

    async def execute(self, context: SkillContext) -> SkillResult:
        raise RuntimeError("boom")

    def diary_status(self, user_id, today, now):
        return ["- contributed line"]


@pytest.fixture
def ctx():
    return SkillContext(trigger="tool_call", user_id=42, args={})


@pytest.mark.asyncio
async def test_refresh_called_when_overridden_and_success(ctx):
    skill = _DiarySkill()
    with patch("mochi.diary.refresh_diary_status") as mock_refresh:
        result = await skill.run(ctx)
    assert result.success
    mock_refresh.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_refresh_skipped_when_not_overridden(ctx):
    skill = _PlainSkill()
    with patch("mochi.diary.refresh_diary_status") as mock_refresh:
        result = await skill.run(ctx)
    assert result.success
    mock_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_skipped_on_failure(ctx):
    skill = _FailingDiarySkill()
    with patch("mochi.diary.refresh_diary_status") as mock_refresh:
        result = await skill.run(ctx)
    assert not result.success
    mock_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_skipped_when_execute_raises(ctx):
    skill = _RaisingDiarySkill()
    with patch("mochi.diary.refresh_diary_status") as mock_refresh:
        result = await skill.run(ctx)
    assert not result.success
    mock_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_exception_swallowed(ctx):
    skill = _DiarySkill()
    with patch("mochi.diary.refresh_diary_status", side_effect=RuntimeError("disk full")):
        result = await skill.run(ctx)
    assert result.success
    assert result.output == "ok"


@pytest.mark.asyncio
async def test_zero_user_id_passes_none():
    """user_id=0 (default) → pass None so refresh falls back to OWNER_USER_ID."""
    ctx = SkillContext(trigger="tool_call", user_id=0, args={})
    skill = _DiarySkill()
    with patch("mochi.diary.refresh_diary_status") as mock_refresh:
        await skill.run(ctx)
    mock_refresh.assert_called_once_with(None)
