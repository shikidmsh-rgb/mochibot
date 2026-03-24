"""Maintenance skill — nightly memory hygiene pipeline.

Runs at MAINTENANCE_HOUR (default 3 AM). Steps:
  1. Diary archive — snapshot + clear
  2. Dedup — merge near-duplicate memory items (uses LLM)
  3. Outdated removal — demote stale low-importance items
  4. Core audit — check core_memory under token budget
  5. Summary — store for morning report

Triggered by heartbeat as a cron skill.
"""

import logging
from datetime import datetime, timezone, timedelta

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.config import (
    TIMEZONE_OFFSET_HOURS, CORE_MEMORY_MAX_TOKENS,
    OWNER_USER_ID,
)

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))


async def run_maintenance(user_id: int = 0) -> dict:
    """Execute nightly maintenance pipeline. Returns summary dict."""
    uid = user_id or OWNER_USER_ID
    results: dict = {}

    # 1. Diary archive
    try:
        from mochi.skills.diary.handler import save_diary_snapshot
        results["diary"] = save_diary_snapshot()
    except Exception as e:
        log.error("Maintenance diary archive failed: %s", e)
        results["diary"] = f"Error: {e}"

    # 2. Dedup (uses LLM via memory_engine)
    try:
        from mochi.memory_engine import deduplicate_memories
        merged = deduplicate_memories(uid)
        results["dedup"] = f"Merged {merged} duplicate(s)"
    except Exception as e:
        log.error("Maintenance dedup failed: %s", e)
        results["dedup"] = f"Error: {e}"

    # 3. Outdated removal
    try:
        from mochi.db import get_stale_memory_items, demote_memory_item
        stale = get_stale_memory_items(uid)
        demoted = 0
        for item in stale:
            demote_memory_item(item["id"])
            demoted += 1
        results["outdated"] = f"Demoted {demoted} stale item(s)"
    except Exception as e:
        log.error("Maintenance outdated removal failed: %s", e)
        results["outdated"] = f"Error: {e}"

    # 4. Core audit
    try:
        from mochi.db import get_core_memory
        core = get_core_memory(uid)
        # Rough token estimate: ~4 chars per token
        est_tokens = len(core) // 4 if core else 0
        if est_tokens > CORE_MEMORY_MAX_TOKENS:
            results["core_audit"] = (
                f"WARNING: core memory ~{est_tokens} tokens "
                f"(budget: {CORE_MEMORY_MAX_TOKENS})"
            )
        else:
            results["core_audit"] = f"OK ({est_tokens}/{CORE_MEMORY_MAX_TOKENS} tokens)"
    except Exception as e:
        log.error("Maintenance core audit failed: %s", e)
        results["core_audit"] = f"Error: {e}"

    # 5. Store summary for morning report
    try:
        from mochi.runtime_state import set_maintenance_summary
        parts = [f"- {k}: {v}" for k, v in results.items()]
        summary = "Nightly maintenance complete:\n" + "\n".join(parts)
        set_maintenance_summary(summary)
        log.info("Maintenance complete: %s", results)
    except Exception as e:
        log.error("Failed to store maintenance summary: %s", e)

    return results


class MaintenanceSkill(Skill):
    """Nightly memory hygiene automation."""

    async def execute(self, context: SkillContext) -> SkillResult:
        from mochi.config import MAINTENANCE_ENABLED
        if not MAINTENANCE_ENABLED:
            return SkillResult(output="Maintenance disabled.", success=True)

        results = await run_maintenance(context.user_id)
        output = "\n".join(f"{k}: {v}" for k, v in results.items())
        return SkillResult(output=output)
