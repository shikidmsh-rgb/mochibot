"""Tests for mochi/tool_router.py — tier resolution, classification, escalation."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

import mochi.tool_router as router


@pytest.fixture(autouse=True)
def reset_router(monkeypatch):
    """Reset router state before each test."""
    monkeypatch.setattr(router, "_metadata_initialized", True)
    monkeypatch.setattr(router, "TOOL_METADATA", {
        "manage_reminder": {"skill": "reminder", "risk_level": "L1"},
        "manage_todo": {"skill": "todo", "risk_level": "L1"},
        "recall_memory": {"skill": "memory", "risk_level": "L0"},
    })
    monkeypatch.setattr(router, "_SKILL_DESCRIPTIONS", {
        "reminder": "Set and manage reminders",
        "todo": "Manage to-do lists",
        "memory": "Store and recall memories",
        "habit": "Track habits",
    })
    monkeypatch.setattr(router, "_SKILL_DEFAULT_TIER", {
        "reminder": "chat",
        "todo": "chat",
        "memory": "chat",
        "habit": "chat",
        "maintenance": "deep",
    })


# ── resolve_tier ──

class TestResolveTier:

    def test_valid_llm_tier(self):
        assert router.resolve_tier(llm_tier="deep") == "deep"

    def test_invalid_llm_tier_falls_through(self):
        assert router.resolve_tier(llm_tier="ultra") == "chat"

    def test_empty_llm_tier_falls_through(self):
        assert router.resolve_tier(llm_tier="") == "chat"

    def test_none_llm_tier_falls_through(self):
        assert router.resolve_tier(llm_tier=None) == "chat"

    def test_infer_from_skills(self):
        result = router.resolve_tier(llm_skills={"reminder"})
        assert result == "chat"

    def test_highest_tier_wins(self):
        result = router.resolve_tier(llm_skills={"reminder", "maintenance"})
        assert result == "deep"

    @patch("mochi.tool_router._get_skill_tier_override")
    def test_admin_override(self, mock_override):
        mock_override.return_value = "deep"
        result = router.resolve_tier(llm_skills={"todo"})
        assert result == "deep"

    def test_default_chat(self):
        assert router.resolve_tier() == "chat"

    def test_unknown_skills_default(self):
        result = router.resolve_tier(llm_skills={"nonexistent_skill"})
        assert result == "chat"


# ── get_tool_meta ──

class TestGetToolMeta:

    def test_known_tool(self):
        meta = router.get_tool_meta("manage_reminder")
        assert meta["skill"] == "reminder"
        assert meta["risk_level"] == "L1"

    def test_unknown_tool(self):
        meta = router.get_tool_meta("nonexistent")
        assert meta["skill"] == "unknown"
        assert meta["risk_level"] == "L0"


# ── keyword_fallback ──

class TestKeywordFallback:

    def test_reminder_english(self):
        result = router.keyword_fallback("remind me to buy milk")
        assert "reminder" in result

    def test_reminder_chinese(self):
        result = router.keyword_fallback("提醒我下午开会")
        assert "reminder" in result

    def test_todo_keyword(self):
        result = router.keyword_fallback("add a task to my todo list")
        assert "todo" in result

    def test_memory_keyword(self):
        result = router.keyword_fallback("remember that I like tea")
        assert "memory" in result

    def test_multiple_matches(self):
        result = router.keyword_fallback("remind me to add this task to todo")
        assert "reminder" in result
        assert "todo" in result

    def test_no_match(self):
        result = router.keyword_fallback("hello how are you")
        assert result == []

    def test_case_insensitive(self):
        result = router.keyword_fallback("REMIND me please")
        assert "reminder" in result


# ── classify_skills_llm ──

class TestClassifySkillsLlm:

    @pytest.mark.asyncio
    async def test_successful_classification(self, monkeypatch):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.content = '{"skills": ["reminder"]}'
        resp.prompt_tokens = 10
        resp.completion_tokens = 5
        resp.total_tokens = 15
        resp.model = "test"
        mock_client.chat.return_value = resp

        with patch("mochi.llm.get_client_for_tier", return_value=mock_client), \
             patch("mochi.db.log_usage"):
            result = await router.classify_skills_llm("remind me to buy milk")

        assert result == ["reminder"]

    @pytest.mark.asyncio
    async def test_json_parse_failure(self, monkeypatch):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.content = "not json"
        resp.prompt_tokens = 10
        resp.completion_tokens = 5
        resp.total_tokens = 15
        resp.model = "test"
        mock_client.chat.return_value = resp

        with patch("mochi.llm.get_client_for_tier", return_value=mock_client), \
             patch("mochi.db.log_usage"):
            result = await router.classify_skills_llm("hello")

        assert result is None

    @pytest.mark.asyncio
    async def test_llm_exception(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.chat.side_effect = Exception("API error")

        with patch("mochi.llm.get_client_for_tier", return_value=mock_client):
            result = await router.classify_skills_llm("hello")

        assert result is None

    @pytest.mark.asyncio
    async def test_no_descriptions_returns_none(self, monkeypatch):
        monkeypatch.setattr(router, "_SKILL_DESCRIPTIONS", {})
        result = await router.classify_skills_llm("hello")
        assert result is None


# ── classify_skills ──

class TestClassifySkills:

    @pytest.mark.asyncio
    async def test_llm_success_used(self, monkeypatch):
        with patch.object(router, "classify_skills_llm", return_value=["todo"]):
            result = await router.classify_skills("add a task")
        assert result == ["todo"]

    @pytest.mark.asyncio
    async def test_llm_empty_triggers_fallback(self, monkeypatch):
        with patch.object(router, "classify_skills_llm", return_value=[]):
            result = await router.classify_skills("remind me to buy milk")
        assert "reminder" in result

    @pytest.mark.asyncio
    async def test_llm_none_triggers_fallback(self, monkeypatch):
        with patch.object(router, "classify_skills_llm", return_value=None):
            result = await router.classify_skills("add a task to my todo")
        assert "todo" in result

    @pytest.mark.asyncio
    async def test_both_fail_returns_empty(self, monkeypatch):
        with patch.object(router, "classify_skills_llm", return_value=None):
            result = await router.classify_skills("hello nice weather")
        assert result == []


# ── validate_escalation ──

class TestValidateEscalation:

    @patch("mochi.skills.get_skill")
    def test_valid_skills(self, mock_get_skill):
        mock_get_skill.return_value = MagicMock()
        result = router.validate_escalation({"skills": "reminder,todo", "reason": "need tools"})
        assert "reminder" in result
        assert "todo" in result

    @patch("mochi.skills.get_skill")
    def test_unknown_filtered(self, mock_get_skill):
        mock_get_skill.side_effect = lambda name: MagicMock() if name == "todo" else None
        result = router.validate_escalation({"skills": "todo,nonexistent"})
        assert result == ["todo"]

    @patch("mochi.skills.get_skill")
    def test_empty_string(self, mock_get_skill):
        result = router.validate_escalation({"skills": ""})
        assert result == []


# ── _build_habit_hint ──

class TestBuildHabitHint:

    def test_empty_habits(self):
        assert router._build_habit_hint(None) == ""
        assert router._build_habit_hint([]) == ""

    def test_with_names(self):
        result = router._build_habit_hint(["Drink Water", "Exercise"])
        assert "Drink Water" in result
        assert "Exercise" in result
        assert "habit" in result.lower()
