"""Shared fixtures for E2E tests.

Provides:
- fresh_db: isolated SQLite database per test
- mock_config: override config values so tests don't need .env
- mock_llm_factory: create scripted MockLLMProvider instances
- discover_skills: one-time skill discovery
- reset_tool_policy: clear rate-limit and deny state between tests
"""

import pytest

from mochi.db import init_db


# ── Database isolation ──

@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Fresh SQLite database for each test."""
    db_path = tmp_path / "e2e_test.db"
    import mochi.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    init_db()
    yield db_path


# ── Config overrides ──

@pytest.fixture(autouse=True)
def mock_config(monkeypatch):
    """Override config values so E2E tests never rely on .env."""
    import mochi.config as cfg
    monkeypatch.setattr(cfg, "OWNER_USER_ID", 1)
    monkeypatch.setattr(cfg, "TOOL_ROUTER_ENABLED", False)
    monkeypatch.setattr(cfg, "TOOL_ESCALATION_ENABLED", False)
    monkeypatch.setattr(cfg, "TOOL_LOOP_MAX_ROUNDS", 5)
    monkeypatch.setattr(cfg, "AI_CHAT_MAX_COMPLETION_TOKENS", 1024)
    monkeypatch.setattr(cfg, "TIMEZONE_OFFSET_HOURS", 0)
    monkeypatch.setattr(cfg, "HEARTBEAT_INTERVAL_MINUTES", 20)
    monkeypatch.setattr(cfg, "AWAKE_HOUR_START", 0)
    monkeypatch.setattr(cfg, "AWAKE_HOUR_END", 24)
    monkeypatch.setattr(cfg, "MAX_DAILY_PROACTIVE", 10)
    monkeypatch.setattr(cfg, "PROACTIVE_COOLDOWN_SECONDS", 0)


# ── Mock LLM factory ──

@pytest.fixture
def mock_llm_factory(monkeypatch):
    """Return a factory that creates MockLLMProvider and patches get_client.

    Usage in tests:
        mock = mock_llm_factory([response1, response2])
        # now ai_client.chat() will use the mock
    """
    from tests.e2e.mock_llm import MockLLMProvider
    from mochi.llm import LLMResponse

    def factory(responses: list[LLMResponse] | None = None):
        mock = MockLLMProvider(responses)

        # Patch the local binding in ai_client (import-time binding trap)
        import mochi.ai_client as ai_client_mod
        monkeypatch.setattr(ai_client_mod, "get_client_for_tier", lambda *a, **kw: mock)

        return mock

    return factory


# ── Skill discovery (session-scoped is not safe with monkeypatch, use module) ──

@pytest.fixture(autouse=True)
def discover_skills():
    """Discover skills once — they register globally and persist."""
    import mochi.skills as skill_registry
    if not skill_registry.get_tools():
        skill_registry.discover()


# ── Tool policy reset ──

@pytest.fixture(autouse=True)
def reset_tool_policy(monkeypatch):
    """Clear tool policy state between tests."""
    import mochi.tool_policy as tp
    monkeypatch.setattr(tp, "_deny_set", set())
    monkeypatch.setattr(tp, "_confirm_set", set())
    monkeypatch.setattr(tp, "_call_log", {})


# ── Heartbeat state reset ──

@pytest.fixture(autouse=True)
def reset_heartbeat_state(monkeypatch):
    """Reset heartbeat module-level state between tests."""
    import mochi.heartbeat as hb
    monkeypatch.setattr(hb, "_state", "AWAKE")
    monkeypatch.setattr(hb, "_last_think_at", None)
    monkeypatch.setattr(hb, "_last_proactive_at", None)
    monkeypatch.setattr(hb, "_proactive_count_today", 0)
    monkeypatch.setattr(hb, "_last_proactive_date", "")
    monkeypatch.setattr(hb, "_prev_observer_raw", {})
    monkeypatch.setattr(hb, "_send_callback", None)
    monkeypatch.setattr(hb, "_wake_reason", None)
    monkeypatch.setattr(hb, "_morning_hold", False)
    monkeypatch.setattr(hb, "_last_sleep_at", None)
    monkeypatch.setattr(hb, "_silent_pause", False)
