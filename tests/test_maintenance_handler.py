"""Tests for mochi/skills/maintenance/handler.py — MaintenanceSkill and run_maintenance."""

import pytest
from unittest.mock import patch, AsyncMock

from mochi.skills.base import SkillContext, SkillResult
from mochi.skills.maintenance.handler import MaintenanceSkill, run_maintenance


def _make_ctx(user_id: int = 1) -> SkillContext:
    return SkillContext(trigger="cron", user_id=user_id, tool_name="", args={})


# All imports inside run_maintenance are deferred (inside function body).
# We must patch at the source module where each function is defined.

_PATCH_ARCHIVE = "mochi.skills.note.handler.archive_notes"
_PATCH_DEDUP = "mochi.memory_engine.deduplicate_memories"
_PATCH_OUTDATED = "mochi.memory_engine.remove_outdated_memories"
_PATCH_SALIENCE = "mochi.memory_engine.rebalance_salience"
_PATCH_CORE = "mochi.db.get_core_memory"
_PATCH_TRASH = "mochi.db.cleanup_old_trash"
_PATCH_SUMMARY = "mochi.runtime_state.set_maintenance_summary"


class TestRunMaintenance:

    @pytest.mark.asyncio
    async def test_full_pipeline_success(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "TRASH_PURGE_DAYS", 30)

        with patch(_PATCH_ARCHIVE, return_value={"status": "ok", "archived": 1}), \
             patch(_PATCH_DEDUP, return_value=2), \
             patch(_PATCH_OUTDATED, return_value={"deleted": 4}), \
             patch(_PATCH_SALIENCE, return_value={"promoted": 1, "demoted": 2}), \
             patch(_PATCH_CORE, return_value="short core"), \
             patch(_PATCH_TRASH, return_value=3), \
             patch(_PATCH_SUMMARY) as mock_summary:
            results = await run_maintenance(user_id=1)

        assert "notes" in results
        assert "dedup" in results
        assert "outdated" in results
        assert "salience" in results
        assert "core_audit" in results
        assert "trash_purge" in results
        mock_summary.assert_called_once()

    @pytest.mark.asyncio
    async def test_individual_step_failure_continues(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "TRASH_PURGE_DAYS", 30)

        with patch(_PATCH_ARCHIVE, return_value={"status": "ok", "archived": 0}), \
             patch(_PATCH_DEDUP, return_value=0), \
             patch(_PATCH_OUTDATED, return_value={"deleted": 0}), \
             patch(_PATCH_SALIENCE, side_effect=RuntimeError("boom")), \
             patch(_PATCH_CORE, return_value="x"), \
             patch(_PATCH_TRASH, return_value=0), \
             patch(_PATCH_SUMMARY):
            results = await run_maintenance(user_id=1)

        assert "Error" in results["salience"]
        assert "dedup" in results
        assert "trash_purge" in results

    @pytest.mark.asyncio
    async def test_notes_archived(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "TRASH_PURGE_DAYS", 30)

        with patch(_PATCH_ARCHIVE, return_value={"status": "ok", "archived": 1}), \
             patch(_PATCH_DEDUP, return_value=0), \
             patch(_PATCH_OUTDATED, return_value={"deleted": 0}), \
             patch(_PATCH_SALIENCE, return_value={"promoted": 0, "demoted": 0}), \
             patch(_PATCH_CORE, return_value="x"), \
             patch(_PATCH_TRASH, return_value=0), \
             patch(_PATCH_SUMMARY):
            results = await run_maintenance(user_id=1)

        assert "1 snapshot" in results["notes"]

    @pytest.mark.asyncio
    async def test_trash_purged(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "TRASH_PURGE_DAYS", 30)

        with patch(_PATCH_ARCHIVE, return_value={"status": "ok", "archived": 0}), \
             patch(_PATCH_DEDUP, return_value=0), \
             patch(_PATCH_OUTDATED, return_value={"deleted": 0}), \
             patch(_PATCH_SALIENCE, return_value={"promoted": 0, "demoted": 0}), \
             patch(_PATCH_CORE, return_value="x"), \
             patch(_PATCH_TRASH, return_value=7), \
             patch(_PATCH_SUMMARY):
            results = await run_maintenance(user_id=1)

        assert "7" in results["trash_purge"]

    @pytest.mark.asyncio
    async def test_summary_stored(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "TRASH_PURGE_DAYS", 30)

        with patch(_PATCH_ARCHIVE, return_value={"status": "ok", "archived": 0}), \
             patch(_PATCH_DEDUP, return_value=0), \
             patch(_PATCH_OUTDATED, return_value={"deleted": 0}), \
             patch(_PATCH_SALIENCE, return_value={"promoted": 0, "demoted": 0}), \
             patch(_PATCH_CORE, return_value="x"), \
             patch(_PATCH_TRASH, return_value=0), \
             patch(_PATCH_SUMMARY) as mock_summary:
            await run_maintenance(user_id=1)

        mock_summary.assert_called_once()
        summary_text = mock_summary.call_args[0][0]
        assert "Nightly maintenance complete" in summary_text

    @pytest.mark.asyncio
    async def test_core_audit_warning(self, monkeypatch):
        import mochi.skills.maintenance.handler as maint_mod
        import mochi.config as cfg
        monkeypatch.setattr(maint_mod, "CORE_MEMORY_MAX_TOKENS", 500)
        monkeypatch.setattr(cfg, "TRASH_PURGE_DAYS", 30)

        with patch(_PATCH_ARCHIVE, return_value={"status": "ok", "archived": 0}), \
             patch(_PATCH_DEDUP, return_value=0), \
             patch(_PATCH_OUTDATED, return_value={"deleted": 0}), \
             patch(_PATCH_SALIENCE, return_value={"promoted": 0, "demoted": 0}), \
             patch(_PATCH_CORE, return_value="a" * 4000), \
             patch(_PATCH_TRASH, return_value=0), \
             patch(_PATCH_SUMMARY):
            results = await run_maintenance(user_id=1)

        assert "WARNING" in results["core_audit"]


class TestMaintenanceSkillExecute:

    @pytest.mark.asyncio
    async def test_disabled(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "MAINTENANCE_ENABLED", False)
        skill = MaintenanceSkill()
        result = await skill.execute(_make_ctx())
        assert result.success is True
        assert "disabled" in result.output.lower()

    @pytest.mark.asyncio
    async def test_enabled_runs_pipeline(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "MAINTENANCE_ENABLED", True)
        mock_run = AsyncMock(return_value={"notes": "ok", "dedup": "Merged 0"})
        with patch("mochi.skills.maintenance.handler.run_maintenance", mock_run):
            skill = MaintenanceSkill()
            result = await skill.execute(_make_ctx(user_id=42))
            mock_run.assert_awaited_once_with(42)
            assert result.success is True
            assert "notes" in result.output
