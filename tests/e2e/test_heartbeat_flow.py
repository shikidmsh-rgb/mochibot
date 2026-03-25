"""E2E tests for heartbeat state management."""

import pytest

from mochi.heartbeat import force_wake, get_state, get_stats, set_send_callback


class TestHeartbeatState:

    def test_force_wake(self, monkeypatch):
        """force_wake() transitions state to AWAKE."""
        import mochi.heartbeat as hb
        monkeypatch.setattr(hb, "_state", "SLEEPING")

        force_wake()

        assert get_state() == "AWAKE"

    def test_get_stats_returns_expected_keys(self):
        """get_stats() returns a dict with all expected fields."""
        stats = get_stats()

        assert "state" in stats
        assert "proactive_today" in stats
        assert "proactive_limit" in stats
        assert isinstance(stats["proactive_today"], int)
        assert isinstance(stats["proactive_limit"], int)

    def test_send_callback_registration(self):
        """set_send_callback() registers the callback."""
        import mochi.heartbeat as hb

        callback_called = False

        async def dummy_callback(user_id: int, text: str):
            nonlocal callback_called
            callback_called = True

        set_send_callback(dummy_callback)

        assert hb._send_callback is dummy_callback
