#!/usr/bin/env python3
"""Dump the exact Think prompt for the latest heartbeat Think round.

Replays heartbeat._think() prompt assembly WITHOUT calling the API.
Outputs the think system prompt, observation text, tool defs, and token estimates.

Usage:
    python scripts/dump_think_prompt.py
    python scripts/dump_think_prompt.py --out /tmp/think_prompt_dump_output.txt
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token)."""
    return len(text) // 4 if text else 0


def _get_recent_heartbeat_logs(limit: int = 30) -> list[dict]:
    """Local helper — query heartbeat_log without modifying db.py."""
    from mochi.db import _connect
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM heartbeat_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


_SKIP_ACTIONS = {"sleeping", "observe_only", "silent_pause", "morning_hold",
                 "cooldown", "rate_limited", "error"}


async def dump() -> str:
    from mochi.config import TIMEZONE_OFFSET_HOURS
    from mochi.prompt_loader import get_prompt
    from mochi.heartbeat import _build_observation_text
    from mochi.model_pool import get_pool

    CST = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

    # Find the latest real Think round (not sleeping/observe_only)
    latest = None
    for row in _get_recent_heartbeat_logs(50):
        action = row.get("action", "")
        if action in _SKIP_ACTIONS:
            continue
        latest = row
        break

    now = datetime.now(CST)
    out: list[str] = []
    out.append(f"=== MOCHIBOT THINK PROMPT DUMP \u2014 {now.strftime('%Y-%m-%d %H:%M:%S')} ===")

    if not latest:
        out.append("No recent Think heartbeat round found in heartbeat_log.")
        out.append("(Need a row with a real Think action, not sleeping/observe_only.)")
        out.append("")
        out.append("=== DONE ===")
        return "\n".join(out)

    # Load Think system prompt
    system_prompt = get_prompt("think_system") or "(think_system prompt not found)"

    # Reconstruct observation — we can't get the exact observation from DB
    # (heartbeat_log doesn't store observations JSON like Mochi does).
    # Instead, show the current observation as a representative snapshot.
    from mochi.config import OWNER_USER_ID
    from mochi.heartbeat import _observe
    observation = await _observe(OWNER_USER_ID)
    obs_text = _build_observation_text(observation)

    pool = get_pool()
    _, model = pool.get_tier("deep"), pool.get_tier_model("deep")

    out.append(
        f"Round: id={latest.get('id')} | action={latest.get('action')} | "
        f"created_at={latest.get('created_at')}"
    )
    out.append(f"Summary: {latest.get('summary', '(none)')}")
    out.append(f"Model tier: deep | model: {model}")
    out.append("")

    # System prompt
    out.append("=" * 60)
    out.append("SYSTEM PROMPT (think_system.md, as sent to API)")
    out.append("=" * 60)
    out.append(system_prompt)
    out.append("")

    # Observation text
    out.append("=" * 60)
    out.append("OBSERVATION TEXT (user content sent to Think)")
    out.append("=" * 60)
    out.append("NOTE: This is a LIVE snapshot of current observation,")
    out.append("not the exact observation from the logged round.")
    out.append("")
    out.append(obs_text)
    out.append("")

    # Messages payload
    out.append("=" * 60)
    out.append("MESSAGES PAYLOAD (Think call)")
    out.append("=" * 60)
    out.append(f"  [1] system: {len(system_prompt)} chars")
    out.append(f"  [2] user:   {len(obs_text)} chars")
    out.append("")

    # Latest Think result
    out.append("=" * 60)
    out.append("LATEST THINK RESULT (from heartbeat_log)")
    out.append("=" * 60)
    out.append(f"  Action:   {latest.get('action', '?')}")
    out.append(f"  Summary:  {latest.get('summary', '(none)')}")
    out.append(f"  State:    {latest.get('state', '?')}")
    out.append(f"  Time:     {latest.get('created_at', '?')}")
    out.append("")

    # Recent heartbeat activity (last 10 rounds)
    recent = _get_recent_heartbeat_logs(10)
    out.append("=" * 60)
    out.append(f"RECENT HEARTBEAT ACTIVITY (last {len(recent)})")
    out.append("=" * 60)
    for r in recent:
        out.append(
            f"  [{r.get('id', '?'):>4}] {r.get('state', '?'):>8} "
            f"| {r.get('action', '?'):<20} "
            f"| {r.get('created_at', '?')}"
        )
    out.append("")

    # Token estimates
    system_tokens = _estimate_tokens(system_prompt)
    obs_tokens = _estimate_tokens(obs_text)
    total = system_tokens + obs_tokens

    out.append("=" * 60)
    out.append("TOKEN ESTIMATES (Think call)")
    out.append("=" * 60)
    out.append(f"  System prompt:  ~{system_tokens} tokens")
    out.append(f"  Observation:    ~{obs_tokens} tokens")
    out.append(f"  Total:          ~{total} tokens")
    out.append("  Note: follow-up rounds (tool results) are larger.")
    out.append("")
    out.append("=== DONE ===")

    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="Dump MochiBot Think prompt")
    parser.add_argument("--out", default=None, help="Output file path")
    args = parser.parse_args()

    result = asyncio.run(dump())

    if args.out:
        Path(args.out).write_text(result, encoding="utf-8")
        print(f"Dumped to {args.out} ({len(result)} chars)")
    else:
        print(result)


if __name__ == "__main__":
    main()
