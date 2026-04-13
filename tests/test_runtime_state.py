"""Tests for mochi/runtime_state.py — get/set/clear, thread safety."""

import pytest
from concurrent.futures import ThreadPoolExecutor

import mochi.runtime_state as rs


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Reset runtime state before each test."""
    monkeypatch.setattr(rs, "_state", rs.RuntimeState())


class TestMaintenanceSummary:

    def test_default_empty(self):
        assert rs.get_maintenance_summary() == ""

    def test_set_and_get(self):
        rs.set_maintenance_summary("done: 5 merged")
        assert rs.get_maintenance_summary() == "done: 5 merged"

    def test_clear(self):
        rs.set_maintenance_summary("something")
        rs.clear_maintenance_summary()
        assert rs.get_maintenance_summary() == ""


class TestUserStatus:

    def test_default_unknown(self):
        assert rs.get_user_status() == "unknown"

    def test_set_and_get(self):
        rs.set_user_status("active")
        assert rs.get_user_status() == "active"

    def test_set_updates_timestamp(self):
        rs.set_user_status("idle")
        assert rs._state.user_status_updated != ""


class TestCustomState:

    def test_get_default_none(self):
        assert rs.get_custom("missing") is None

    def test_get_custom_default(self):
        assert rs.get_custom("missing", 42) == 42

    def test_set_and_get(self):
        rs.set_custom("key1", "val1")
        assert rs.get_custom("key1") == "val1"

    def test_clear(self):
        rs.set_custom("key1", "val1")
        rs.clear_custom("key1")
        assert rs.get_custom("key1") is None

    def test_keys_isolated(self):
        rs.set_custom("a", 1)
        rs.set_custom("b", 2)
        assert rs.get_custom("a") == 1
        assert rs.get_custom("b") == 2
        rs.clear_custom("a")
        assert rs.get_custom("a") is None
        assert rs.get_custom("b") == 2


class TestThreadSafety:

    def test_concurrent_access(self):
        """Multiple threads writing and reading without errors."""
        def worker(n):
            rs.set_custom(f"key_{n}", n)
            rs.set_maintenance_summary(f"summary_{n}")
            rs.set_user_status(f"status_{n}")
            return rs.get_custom(f"key_{n}")

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(worker, range(20)))

        # All workers should have returned their own value
        for i, result in enumerate(results):
            assert result == i
