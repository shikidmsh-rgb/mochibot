#!/usr/bin/env python3
"""Dump the exact prompt input MochiBot receives for the latest user message.

Replays the chat() assembly logic WITHOUT calling the API.
Outputs the full system prompt (rendered), history, tool defs, and token estimates.

Usage:
    python scripts/dump_prompt.py
    python scripts/dump_prompt.py --out /tmp/prompt_dump_output.txt
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


async def dump() -> str:
    from mochi.config import (
        MAX_HISTORY_TURNS,
        OWNER_USER_ID,
        TIMEZONE_OFFSET_HOURS,
        TOOL_ESCALATION_ENABLED,
        TOOL_ROUTER_ENABLED,
    )
    from mochi.db import (
        _connect,
        get_conversation_context,
        get_skill_mode,
    )
    from mochi.core_store import read_core
    from mochi.ai_client import (
        _build_system_prompt, _expand_history, _retrieve_memories_for_turn,
    )
    from mochi.skills.habit.queries import list_habits
    import mochi.skills as skill_registry
    from mochi.model_pool import get_pool
    from mochi.tool_policy import filter_tools

    skill_registry.discover()

    CST = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))
    user_id = OWNER_USER_ID

    conversation_context = get_conversation_context(
        user_id,
        MAX_HISTORY_TURNS,
        include_summary=True,
    )
    history = [
        *conversation_context["overflow"],
        *conversation_context["recent"],
        *conversation_context["trailing"],
    ]

    # Find latest user message
    latest_user_msg = ""
    for msg in reversed(history or []):
        if msg.get("role") == "user":
            latest_user_msg = msg.get("content", "")
            if isinstance(latest_user_msg, list):
                latest_user_msg = " ".join(
                    p.get("text", "") for p in latest_user_msg if isinstance(p, dict)
                )
            break

    # Read last chat call from usage_log
    last_call = None
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT model, purpose, prompt_tokens, completion_tokens, "
            "       total_tokens, tool_name, cost_usd, created_at "
            "FROM usage_log WHERE purpose LIKE 'chat:%' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            last_call = {
                "model": row[0], "purpose": row[1],
                "prompt_tokens": row[2], "completion_tokens": row[3],
                "total_tokens": row[4], "tools_called": row[5],
                "cost": row[6], "time": row[7],
            }
        conn.close()
    except Exception:
        pass

    tier = "main"

    # Replay chat() assembly — same logic as ai_client.chat()
    habits = await asyncio.to_thread(list_habits, user_id)
    recalled_memories = _retrieve_memories_for_turn(latest_user_msg, user_id)

    from mochi.turn_tool_policy import build_turn_tool_plan

    plan = build_turn_tool_plan("telegram")
    tools = list(plan.resident_definitions)
    if plan.router_enabled:
        from mochi.tool_router import classify_skills

        selected = await classify_skills(
            latest_user_msg,
            user_id=user_id,
            habits=habits,
            transport="telegram",
            catalog=plan.router_descriptions,
        )
        selected = plan.filter_router_selection(selected)
        tools.extend(skill_registry.get_tools_by_names(
            selected,
            transport="telegram",
            loads={"routed"},
        ))
    escalation_available = plan.request_tools_enabled

    tools = filter_tools(tools)
    tool_names_list = [t["function"]["name"] for t in tools if "function" in t]
    capability_context = skill_registry.get_capability_context_for_tools(
        tool_names_list,
    )
    requestable_tools = (
        skill_registry.get_requestable_tool_lines(
            tool_names_list, transport="telegram",
        )
        if escalation_available else ""
    )
    core_memory = read_core()

    system_prompt = _build_system_prompt(
        user_id, capability_context=capability_context,
        requestable_tools=requestable_tools,
        tool_names=tool_names_list,
        core_memory=core_memory, habits=habits,
        recalled_memories=recalled_memories,
        conv_summary=conversation_context["summary"],
    )

    # Build messages array (same as chat())
    formatted_history = _expand_history(history)
    messages = [{"role": "system", "content": system_prompt}]
    if formatted_history:
        messages.extend(formatted_history)

    # ── Output ──
    now = datetime.now(CST)
    out: list[str] = []
    out.append(f"=== MOCHIBOT PROMPT DUMP — {now.strftime('%Y-%m-%d %H:%M:%S')} ===")

    # Header
    header_parts = [f"User: {user_id}", f"Tier: {tier}"]
    if last_call and last_call.get("model"):
        header_parts.append(f"Model: {last_call['model']}")
    out.append(" | ".join(header_parts))
    out.append(f"Latest user message: {latest_user_msg[:100]}")
    out.append("")

    # Last call info (from DB)
    out.append("=" * 60)
    out.append("LAST CALL (from usage_log)")
    out.append("=" * 60)
    if last_call:
        out.append(f"  Time:       {last_call['time']}")
        out.append(f"  Purpose:    {last_call['purpose']}")
        out.append(f"  Model:      {last_call['model']}")
        out.append(f"  Tools:      {last_call['tools_called'] or '(none)'}")
        out.append(f"  Tokens:     {last_call['prompt_tokens']}\u2192{last_call['completion_tokens']} ({last_call['total_tokens']} total)")
        if last_call.get("cost"):
            out.append(f"  Cost:       ${last_call['cost']:.4f}")
    else:
        out.append("  (no chat:* call found in usage_log)")
    out.append("")

    # Tier → model mapping
    pool = get_pool()
    out.append("=" * 60)
    out.append("TIER \u2192 MODEL MAPPING")
    out.append("=" * 60)
    for t in ("main", "lite"):
        out.append(f"  {t:>10} \u2192 {pool.get_tier_model(t)}")
    out.append("")

    # System prompt (rendered)
    out.append("=" * 60)
    out.append("SYSTEM PROMPT (rendered, as sent to API)")
    out.append("=" * 60)
    out.append(system_prompt)
    out.append("")

    # History
    out.append("=" * 60)
    out.append(f"HISTORY ({len(formatted_history)} messages)")
    out.append("=" * 60)
    for i, msg in enumerate(formatted_history, 1):
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "[multimodal content]"
        if content is None:
            content = "(tool_calls)"
        preview = str(content)[:120].replace("\n", " ")
        out.append(f"  [{i:>2}] {role}: {preview}")
    out.append("")

    # Tool definitions
    out.append("=" * 60)
    out.append(f"TOOL DEFINITIONS ({len(tools) if tools else 0} tools)")
    out.append("=" * 60)
    if tools:
        for t in tools:
            fn = t.get("function", {})
            out.append(f"  - {fn.get('name')}: {fn.get('description', '')[:80]}")
    else:
        out.append("  (none)")
    out.append("")

    # Retrieved memories
    out.append("=" * 60)
    out.append(f"RETRIEVED MEMORIES ({len(recalled_memories)})")
    out.append("=" * 60)
    if recalled_memories:
        for m in recalled_memories:
            out.append(
                f"  - {m.get('text')} "
                f"(score={m.get('score')}, evidence={m.get('evidence_start')})"
            )
    else:
        out.append("  (none)")
    out.append("")

    # Token estimates
    sys_tokens = _estimate_tokens(system_prompt)
    hist_tokens = sum(
        _estimate_tokens(str(m.get("content", "") or ""))
        for m in formatted_history
    )
    tool_tokens = _estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    core_tokens = _estimate_tokens(core_memory)

    out.append("=" * 60)
    out.append("TOKEN ESTIMATES")
    out.append("=" * 60)
    out.append(f"  System prompt:  ~{sys_tokens} tokens")
    out.append(f"    Core memory:  ~{core_tokens} tokens (included in system)")
    out.append(f"  History:        ~{hist_tokens} tokens")
    out.append(f"  Tool defs:      ~{tool_tokens} tokens")
    out.append(f"  Total:          ~{sys_tokens + hist_tokens + tool_tokens} tokens")
    out.append("")
    out.append("=== DONE ===")

    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="Dump MochiBot chat prompt")
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
