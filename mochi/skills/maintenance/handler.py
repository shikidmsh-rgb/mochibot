"""Maintenance skill — nightly memory hygiene pipeline.

Runs at MAINTENANCE_HOUR (default 3 AM). Steps:
  1. Diary archive — snapshot + clear
  2. Dedup — merge near-duplicate memory items (uses LLM)
  3. Outdated removal — LLM-based detection of stale memories
  4. Salience rebalance — promote/demote importance levels (uses LLM)
  5. Core audit — check core_memory under token budget
  6. Trash purge — hard-delete old trash items
  7. Summary — store for morning report

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

    # 1. Diary archive — handled by heartbeat nightly tick (mochi/heartbeat.py)
    results["diary"] = "Handled by heartbeat"

    # 2. Dedup (uses LLM via memory_engine)
    try:
        from mochi.memory_engine import deduplicate_memories
        merged = deduplicate_memories(uid)
        results["dedup"] = f"Merged {merged} duplicate(s)"
    except Exception as e:
        log.error("Maintenance dedup failed: %s", e)
        results["dedup"] = f"Error: {e}"

    # 3. Outdated removal (LLM-based)
    try:
        from mochi.memory_engine import remove_outdated_memories
        outdated = remove_outdated_memories(uid)
        results["outdated"] = f"Deleted {outdated.get('deleted', 0)} outdated item(s)"
    except Exception as e:
        log.error("Maintenance outdated removal failed: %s", e)
        results["outdated"] = f"Error: {e}"

    # 4. Salience rebalance (LLM-based promote/demote)
    try:
        from mochi.memory_engine import rebalance_salience
        salience = rebalance_salience(uid)
        results["salience"] = (
            f"Promoted {salience.get('promoted', 0)}, "
            f"demoted {salience.get('demoted', 0)}"
        )
    except Exception as e:
        log.error("Maintenance salience rebalance failed: %s", e)
        results["salience"] = f"Error: {e}"

    # 5. Core audit
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

    # 6. Trash purge
    try:
        from mochi.db import cleanup_old_trash
        from mochi.config import TRASH_PURGE_DAYS
        purged = cleanup_old_trash(TRASH_PURGE_DAYS)
        results["trash_purge"] = f"Purged {purged} old trash item(s)"
    except Exception as e:
        log.error("Maintenance trash purge failed: %s", e)
        results["trash_purge"] = f"Error: {e}"

    # 7. Store summary for morning report
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
