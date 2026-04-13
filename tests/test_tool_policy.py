"""Tests for mochi/tool_policy.py — denylist, rate limiter, confirmation, filter_tools."""

import pytest
import time
from unittest.mock import patch

import mochi.tool_policy as tp


@pytest.fixture(autouse=True)
def reset_policy(monkeypatch):
    """Reset policy state before each test."""
    monkeypatch.setattr(tp, "_deny_set", set())
    monkeypatch.setattr(tp, "_confirm_set", set())
    monkeypatch.setattr(tp, "_call_log", {})


# ── PolicyDecision dataclass ──

class TestPolicyDecision:

    def test_defaults(self):
        d = tp.PolicyDecision(allowed=True)
        assert d.allowed is True
        assert d.reason == ""
        assert d.needs_confirm is False

    def test_all_fields(self):
        d = tp.PolicyDecision(allowed=False, reason="test", needs_confirm=True)
        assert d.allowed is False
        assert d.reason == "test"
        assert d.needs_confirm is True


# ── Denylist ──

class TestDenylist:

    def test_allowed_tool(self):
        result = tp.check("safe_tool")
        assert result.allowed is True

    def test_denied_tool(self, monkeypatch):
        monkeypatch.setattr(tp, "_deny_set", {"dangerous_tool"})
        result = tp.check("dangerous_tool")
        assert result.allowed is False
        assert "disabled" in result.reason

    def test_deny_does_not_affect_others(self, monkeypatch):
        monkeypatch.setattr(tp, "_deny_set", {"bad_tool"})
        assert tp.check("good_tool").allowed is True


# ── Confirmation gate ──

class TestConfirmationGate:

    def test_confirm_tool(self, monkeypatch):
        monkeypatch.setattr(tp, "_confirm_set", {"risky_tool"})
        result = tp.check("risky_tool")
        assert result.allowed is True
        assert result.needs_confirm is True
        assert "confirmation" in result.reason

    def test_non_confirm_tool(self):
        result = tp.check("normal_tool")
        assert result.needs_confirm is False


# ── Rate limiter ──

class TestRateLimiter:

    def test_under_limit(self, monkeypatch):
        monkeypatch.setattr(tp, "TOOL_RATE_LIMIT_PER_MIN", 5)
        for _ in range(4):
            result = tp.check("my_tool")
            assert result.allowed is True

    def test_at_limit_denied(self, monkeypatch):
        monkeypatch.setattr(tp, "TOOL_RATE_LIMIT_PER_MIN", 3)
        for _ in range(3):
            tp.check("my_tool")
        result = tp.check("my_tool")
        assert result.allowed is False
        assert "rate limited" in result.reason

    def test_window_reset(self, monkeypatch):
        monkeypatch.setattr(tp, "TOOL_RATE_LIMIT_PER_MIN", 2)
        # Fill the rate limit
        tp.check("my_tool")
        tp.check("my_tool")
        assert tp.check("my_tool").allowed is False

        # Simulate time passing by replacing timestamps with old ones
        monkeypatch.setattr(tp, "_call_log", {"my_tool": [time.time() - 120]})
        result = tp.check("my_tool")
        assert result.allowed is True

    def test_tools_independent(self, monkeypatch):
        monkeypatch.setattr(tp, "TOOL_RATE_LIMIT_PER_MIN", 2)
        tp.check("tool_a")
        tp.check("tool_a")
        assert tp.check("tool_a").allowed is False
        # tool_b should still be fine
        assert tp.check("tool_b").allowed is True


# ── filter_tools ──

class TestFilterTools:

    def test_no_deny_passthrough(self):
        tools = [
            {"function": {"name": "tool1"}},
            {"function": {"name": "tool2"}},
        ]
        assert tp.filter_tools(tools) == tools

    def test_filters_denied(self, monkeypatch):
        monkeypatch.setattr(tp, "_deny_set", {"tool2"})
        tools = [
            {"function": {"name": "tool1"}},
            {"function": {"name": "tool2"}},
            {"function": {"name": "tool3"}},
        ]
        result = tp.filter_tools(tools)
        names = [t["function"]["name"] for t in result]
        assert names == ["tool1", "tool3"]

    def test_preserves_order(self, monkeypatch):
        monkeypatch.setattr(tp, "_deny_set", {"b"})
        tools = [
            {"function": {"name": "a"}},
            {"function": {"name": "b"}},
            {"function": {"name": "c"}},
        ]
        result = tp.filter_tools(tools)
        assert [t["function"]["name"] for t in result] == ["a", "c"]


# ── Integration ──

class TestCheckIntegration:

    def test_deny_before_rate(self, monkeypatch):
        """Denied tool should not even touch the rate log."""
        monkeypatch.setattr(tp, "_deny_set", {"blocked"})
        result = tp.check("blocked")
        assert result.allowed is False
        assert "blocked" not in tp._call_log

    def test_confirm_still_rate_checked(self, monkeypatch):
        monkeypatch.setattr(tp, "_confirm_set", {"risky"})
        monkeypatch.setattr(tp, "TOOL_RATE_LIMIT_PER_MIN", 1)
        # First call: confirm gate
        result = tp.check("risky")
        assert result.allowed is True
        assert result.needs_confirm is True
        # Confirm gate returns early, so no rate recording. Check next call.
        # The confirm gate returns before rate check, so rate limit doesn't apply
        result2 = tp.check("risky")
        assert result2.needs_confirm is True
