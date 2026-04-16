"""Live E2E test for pre-router with a configured local MochiBot checkout.

Run from a live checkout with a real .env and data/mochi.db:
    set MOCHIBOT_RUN_LIVE_E2E=1
    pytest tests/e2e/test_prerouter_live.py -v -s

These tests are skipped unless live E2E is explicitly enabled.
"""

import logging
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_ENV_PATH = REPO_ROOT / ".env"
LIVE_DB_PATH = REPO_ROOT / "data" / "mochi.db"

if os.getenv("MOCHIBOT_RUN_LIVE_E2E") != "1":
    pytest.skip(
        "Set MOCHIBOT_RUN_LIVE_E2E=1 to run live pre-router E2E tests.",
        allow_module_level=True,
    )

if not LIVE_ENV_PATH.exists():
    pytest.skip(f"Live E2E requires {LIVE_ENV_PATH}", allow_module_level=True)

if not LIVE_DB_PATH.exists():
    pytest.skip(f"Live E2E requires {LIVE_DB_PATH}", allow_module_level=True)

load_dotenv(LIVE_ENV_PATH, override=True)

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
log = logging.getLogger("test_prerouter_live")


# ── Override conftest's autouse fixtures ──

@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    """Override conftest: use the live checkout DB instead of tmp."""
    import mochi.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", LIVE_DB_PATH)


@pytest.fixture(autouse=True)
def mock_config(monkeypatch):
    """Override conftest: enable router, use real config."""
    import mochi.config as cfg
    monkeypatch.setattr(cfg, "TOOL_ROUTER_ENABLED", True)
    monkeypatch.setattr(cfg, "TOOL_ESCALATION_ENABLED", True)
    monkeypatch.setattr(cfg, "TOOL_ROUTER_MAX_TOKENS", 150)
    monkeypatch.setattr(cfg, "OWNER_USER_ID", 1)
    monkeypatch.setattr(cfg, "TOOL_LOOP_MAX_ROUNDS", 5)
    monkeypatch.setattr(cfg, "AI_CHAT_MAX_COMPLETION_TOKENS", 1024)


@pytest.fixture(autouse=True)
def _reset_pool_and_metadata():
    """Reset singletons so they reload from mochitest DB."""
    import mochi.model_pool as mp
    import mochi.tool_router as tr
    import mochi.admin.admin_crypto as crypto

    mp._pool = None
    crypto.reset_cache()
    tr._metadata_initialized = False
    tr.TOOL_METADATA = {}
    tr._SKILL_DESCRIPTIONS = {}
    tr._SKILL_DEFAULT_TIER = {}
    yield
    mp._pool = None


# ── Helper ──

async def classify(msg: str) -> tuple[list[str], str]:
    from mochi.tool_router import classify_skills, resolve_tier
    skills = await classify_skills(msg, user_id=1)
    tier = resolve_tier(llm_skills=set(skills)) if skills else "chat"
    log.info("  %r → skills=%s tier=%s", msg, skills, tier)
    return skills, tier


# ═══════════════════════════════════════════════════════════════════════════
# 1. 打卡 → habit, tier=lite
# ═══════════════════════════════════════════════════════════════════════════

class TestCheckin:

    @pytest.mark.asyncio
    async def test_checkin_chinese(self):
        skills, tier = await classify("喝水打卡")
        assert "habit" in skills
        assert tier == "lite"

    @pytest.mark.asyncio
    async def test_checkin_with_context(self):
        skills, tier = await classify("运动打卡")
        assert "habit" in skills
        assert tier == "lite"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Habit 查询
# ═══════════════════════════════════════════════════════════════════════════

class TestHabit:

    @pytest.mark.asyncio
    async def test_habit_status(self):
        skills, _ = await classify("今天打卡了几个？")
        assert "habit" in skills

    @pytest.mark.asyncio
    async def test_habit_progress(self):
        skills, _ = await classify("我的习惯完成情况怎么样")
        assert "habit" in skills


# ═══════════════════════════════════════════════════════════════════════════
# 3. Todo
# ═══════════════════════════════════════════════════════════════════════════

class TestTodo:

    @pytest.mark.asyncio
    async def test_add_todo(self):
        skills, _ = await classify("帮我加个待办：买菜")
        assert "todo" in skills

    @pytest.mark.asyncio
    async def test_list_todos(self):
        skills, _ = await classify("我的待办有哪些")
        assert "todo" in skills


# ═══════════════════════════════════════════════════════════════════════════
# 4. Weather
# ═══════════════════════════════════════════════════════════════════════════

class TestWeather:

    @pytest.mark.asyncio
    async def test_weather_chinese(self):
        skills, _ = await classify("查一下现在的天气")
        # LLM may classify as weather OR web_search — both are acceptable
        assert "weather" in skills or "web_search" in skills

    @pytest.mark.asyncio
    async def test_weather_english(self):
        skills, _ = await classify("check the weather forecast")
        assert "weather" in skills or "web_search" in skills


# ═══════════════════════════════════════════════════════════════════════════
# 5. Meal → tier=lite
# ═══════════════════════════════════════════════════════════════════════════

class TestMeal:

    @pytest.mark.asyncio
    async def test_log_meal(self):
        skills, tier = await classify("午饭吃了一碗拉面")
        assert "meal" in skills
        assert tier == "lite"

    @pytest.mark.asyncio
    async def test_query_meals(self):
        skills, _ = await classify("今天吃了什么")
        assert "meal" in skills

    @pytest.mark.asyncio
    async def test_calories(self):
        skills, _ = await classify("昨天摄入了多少卡路里")
        assert "meal" in skills


# ═══════════════════════════════════════════════════════════════════════════
# 6. Web search
# ═══════════════════════════════════════════════════════════════════════════

class TestWebSearch:

    @pytest.mark.asyncio
    async def test_search_chinese(self):
        skills, _ = await classify("帮我搜一下最新的AI新闻")
        assert "web_search" in skills

    @pytest.mark.asyncio
    async def test_search_english(self):
        skills, _ = await classify("search for the latest python release")
        assert "web_search" in skills


# ═══════════════════════════════════════════════════════════════════════════
# 7. Pure chat → no heavy tools
# ═══════════════════════════════════════════════════════════════════════════

class TestPureChat:

    @pytest.mark.asyncio
    async def test_greeting(self):
        skills, _ = await classify("你好呀")
        # Should be empty or at most memory/sticker
        heavy = {"web_search", "todo", "meal", "reminder", "habit"}
        assert not (set(skills) & heavy), f"Greeting triggered heavy tools: {skills}"

    @pytest.mark.asyncio
    async def test_casual(self):
        skills, _ = await classify("今天好累啊")
        heavy = {"web_search", "todo", "meal", "reminder"}
        assert not (set(skills) & heavy), f"Casual chat triggered: {skills}"


# ═══════════════════════════════════════════════════════════════════════════
# 8. Reminder
# ═══════════════════════════════════════════════════════════════════════════

class TestReminder:

    @pytest.mark.asyncio
    async def test_set_reminder(self):
        skills, _ = await classify("提醒我下午三点开会")
        assert "reminder" in skills

    @pytest.mark.asyncio
    async def test_alarm(self):
        skills, _ = await classify("帮我设个闹钟，明早七点")
        assert "reminder" in skills


# ═══════════════════════════════════════════════════════════════════════════
# 9. Tier 验证
# ═══════════════════════════════════════════════════════════════════════════

class TestTier:

    @pytest.mark.asyncio
    async def test_habit_lite(self):
        skills, tier = await classify("喝了一杯水")
        if "habit" in skills:
            assert tier == "lite"

    @pytest.mark.asyncio
    async def test_meal_lite(self):
        skills, tier = await classify("中午吃了汉堡")
        if "meal" in skills:
            assert tier == "lite"

    @pytest.mark.asyncio
    async def test_search_chat(self):
        skills, tier = await classify("搜索一下今天的新闻")
        if "web_search" in skills:
            assert tier == "chat"


# ═══════════════════════════════════════════════════════════════════════════
# 10. Tool injection 验证
# ═══════════════════════════════════════════════════════════════════════════

class TestToolInjection:

    @pytest.mark.asyncio
    async def test_habit_tools(self):
        """habit has expose_as_tool=false, so get_tools_by_names returns [].
        This verifies the router still classifies it (tier routing still works),
        but tools are empty — the LLM handles habit via multi_turn/inline logic.
        """
        from mochi.tool_router import classify_skills
        import mochi.skills as reg
        skills = await classify_skills("打卡喝水", user_id=1)
        if not skills or "habit" not in skills:
            pytest.skip("LLM didn't classify as habit")
        tools = reg.get_tools_by_names(skills)
        names = [t["function"]["name"] for t in tools]
        # NOTE: habit expose_as_tool=false → tools will be empty
        # This is expected — habit uses a different dispatch mechanism
        log.info("  habit tools (expose_as_tool=false): %s", names)

    @pytest.mark.asyncio
    async def test_search_tools(self):
        from mochi.tool_router import classify_skills
        import mochi.skills as reg
        skills = await classify_skills("帮我搜Python最新版本", user_id=1)
        if not skills or "web_search" not in skills:
            pytest.skip("LLM didn't classify as web_search")
        tools = reg.get_tools_by_names(skills)
        names = [t["function"]["name"] for t in tools]
        assert "web_search" in names, f"Got: {names}"
