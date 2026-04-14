"""E2E test: chat_proactive with real LLM (uses mochitest environment).

Requires:
  - mochitest deployed at M:/mochitest/mochibot/ with models configured via admin panel
  - Run: python tests/e2e/test_chat_proactive_e2e.py
"""

import asyncio
import os
import sys

# Fix Windows console encoding
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Bootstrap: point at mochitest so config reads the correct DB
MOCHITEST_ROOT = os.path.join("M:", os.sep, "mochitest", "mochibot")
sys.path.insert(0, MOCHITEST_ROOT)
os.chdir(MOCHITEST_ROOT)

# Force .env loading from mochitest dir
from dotenv import load_dotenv
load_dotenv(os.path.join(MOCHITEST_ROOT, ".env"), override=True)


async def main():
    from mochi.config import OWNER_USER_ID, DB_PATH
    print(f"DB_PATH = {DB_PATH}")
    print(f"OWNER_USER_ID = {OWNER_USER_ID}")

    # Verify DB models are loaded
    from mochi.admin.admin_db import get_tier_effective_config
    tiers = get_tier_effective_config()
    print("\n--- Tier config ---")
    for tier, cfg in tiers.items():
        print(f"  {tier}: provider={cfg.get('provider')}, model={cfg.get('model')}, assigned={cfg.get('assigned_name')}")

    # Verify get_client_for_tier can resolve chat
    from mochi.llm import get_client_for_tier
    try:
        client = get_client_for_tier("chat")
        print(f"\nchat client OK: {client.provider_name()}")
    except Exception as e:
        print(f"\nFAILED to get chat client: {e}")
        return

    # ── Test 1: habit_nudge finding (un-skippable) ──
    print("\n═══ Test 1: habit_nudge (should NOT skip) ═══")
    from mochi.ai_client import chat_proactive
    findings = [
        {"topic": "habit_nudge", "summary": "用户今天还没吃药，习惯'吃药'已逾期 2 小时", "urgency": "high"},
    ]
    result = await chat_proactive(findings, user_id=OWNER_USER_ID)
    print(f"Result: {result!r}")
    assert result is not None, "habit_nudge should not return None"
    assert result != "[SKIP]", "habit_nudge is un-skippable, should not be [SKIP]"
    print("PASS: habit_nudge produced a message")

    # ── Test 2: general finding (may be skipped) ──
    print("\n═══ Test 2: general (may skip) ═══")
    findings2 = [
        {"topic": "general", "summary": "天气晴朗，适合出门走走"},
    ]
    result2 = await chat_proactive(findings2, user_id=OWNER_USER_ID)
    print(f"Result: {result2!r}")
    if result2 == "[SKIP]":
        print("OK: general finding was skipped (allowed)")
    elif result2:
        print("OK: general finding produced a message")
    else:
        print("WARN: returned None (LLM error?)")

    # ── Test 3: sleep_transition ──
    print("\n═══ Test 3: sleep_transition (should NOT skip) ═══")
    findings3 = [
        {"topic": "sleep_transition", "summary": "用户已沉默1.5小时，深夜静默，大概率睡着了"},
    ]
    result3 = await chat_proactive(findings3, user_id=OWNER_USER_ID)
    print(f"Result: {result3!r}")
    assert result3 is not None, "sleep_transition should not return None"
    assert result3 != "[SKIP]", "sleep_transition is un-skippable"
    print("PASS: sleep_transition produced a goodnight message")

    # ── Test 4: multiple findings batched ──
    print("\n═══ Test 4: mixed batch ═══")
    findings4 = [
        {"topic": "habit_nudge", "summary": "运动习惯今天还没打卡", "urgency": "low"},
        {"topic": "general", "summary": "用户今天很活跃，已发 20 条消息"},
    ]
    result4 = await chat_proactive(findings4, user_id=OWNER_USER_ID)
    print(f"Result: {result4!r}")
    assert result4 is not None, "batch with habit_nudge should not return None"
    assert result4 != "[SKIP]", "batch contains un-skippable habit_nudge"
    print("PASS: mixed batch produced a message")

    print("\n══════════════════════════════")
    print("All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
