"""Live E2E tests for chat_proactive with a configured local MochiBot checkout.

Run from a live checkout with a real .env and data/mochi.db:
    set MOCHIBOT_RUN_LIVE_E2E=1
    pytest tests/e2e/test_chat_proactive_e2e.py -v -s

These tests are skipped unless live E2E is explicitly enabled.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_ENV_PATH = REPO_ROOT / ".env"
LIVE_DB_PATH = REPO_ROOT / "data" / "mochi.db"

if os.getenv("MOCHIBOT_RUN_LIVE_E2E") != "1":
    pytest.skip(
        "Set MOCHIBOT_RUN_LIVE_E2E=1 to run live proactive-chat E2E tests.",
        allow_module_level=True,
    )

if not LIVE_ENV_PATH.exists():
    pytest.skip(f"Live E2E requires {LIVE_ENV_PATH}", allow_module_level=True)

if not LIVE_DB_PATH.exists():
    pytest.skip(f"Live E2E requires {LIVE_DB_PATH}", allow_module_level=True)

load_dotenv(LIVE_ENV_PATH, override=True)


def _assert_live_chat_setup() -> int:
    from mochi.config import DB_PATH, OWNER_USER_ID
    from mochi.admin.admin_db import get_tier_effective_config
    from mochi.llm import get_client_for_tier

    assert Path(DB_PATH).exists(), f"Configured DB does not exist: {DB_PATH}"
    assert OWNER_USER_ID, "OWNER_USER_ID must be configured for live E2E"

    tiers = get_tier_effective_config()
    assert "chat" in tiers, f"Tier config missing 'chat': {tiers}"

    client = get_client_for_tier("chat")
    assert client is not None, "Live chat tier could not resolve a client"
    return OWNER_USER_ID


async def _run_chat_proactive(findings: list[dict]) -> str | None:
    from mochi.ai_client import chat_proactive

    owner_user_id = _assert_live_chat_setup()
    return await chat_proactive(findings, user_id=owner_user_id)


def test_live_chat_client_resolves():
    _assert_live_chat_setup()


@pytest.mark.asyncio
async def test_habit_nudge_produces_message():
    findings = [
        {"topic": "habit_nudge", "summary": "用户今天还没吃药，习惯'吃药'已逾期 2 小时", "urgency": "high"},
    ]
    result = await _run_chat_proactive(findings)
    assert result is not None, "habit_nudge should not return None"
    assert result != "[SKIP]", "habit_nudge is un-skippable, should not be [SKIP]"


@pytest.mark.asyncio
async def test_general_finding_may_skip_or_reply():
    findings2 = [
        {"topic": "general", "summary": "天气晴朗，适合出门走走"},
    ]
    result2 = await _run_chat_proactive(findings2)
    assert result2 == "[SKIP]" or bool(result2), "general finding should skip or reply"


@pytest.mark.asyncio
async def test_sleep_transition_produces_message():
    findings3 = [
        {"topic": "sleep_transition", "summary": "用户已沉默1.5小时，深夜静默，大概率睡着了"},
    ]
    result3 = await _run_chat_proactive(findings3)
    assert result3 is not None, "sleep_transition should not return None"
    assert result3 != "[SKIP]", "sleep_transition is un-skippable"


@pytest.mark.asyncio
async def test_mixed_batch_with_habit_nudge_produces_message():
    findings4 = [
        {"topic": "habit_nudge", "summary": "运动习惯今天还没打卡", "urgency": "low"},
        {"topic": "general", "summary": "用户今天很活跃，已发 20 条消息"},
    ]
    result4 = await _run_chat_proactive(findings4)
    assert result4 is not None, "batch with habit_nudge should not return None"
    assert result4 != "[SKIP]", "batch contains un-skippable habit_nudge"
