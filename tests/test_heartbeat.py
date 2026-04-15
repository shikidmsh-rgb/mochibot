"""Tests for heartbeat state machine and decision functions.

Focuses on pure decision functions (state transitions, sleep detection,
silent pause), NOT the async heartbeat loop.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import mochi.heartbeat as hb


@pytest.fixture(autouse=True)
def reset_heartbeat_globals(monkeypatch):
    """Reset all heartbeat module-level globals between tests.

    Pattern from tests/e2e/conftest.py lines 95-109.
    """
    monkeypatch.setattr(hb, "_state", "AWAKE")
    monkeypatch.setattr(hb, "_state_changed_at", datetime.now(timezone.utc))
    monkeypatch.setattr(hb, "_last_think_at", None)
    monkeypatch.setattr(hb, "_last_proactive_at", None)
    monkeypatch.setattr(hb, "_proactive_count_today", 0)
    monkeypatch.setattr(hb, "_last_proactive_date", "")
    monkeypatch.setattr(hb, "_last_maintenance_date", "")
    monkeypatch.setattr(hb, "_prev_observer_raw", {})
    monkeypatch.setattr(hb, "_send_callback", None)
    monkeypatch.setattr(hb, "_wake_reason", None)
    monkeypatch.setattr(hb, "_last_sleep_at", None)
    monkeypatch.setattr(hb, "_silent_pause", False)


class TestStateMachine:

    def test_wake_sleeping_to_awake(self):
        """SLEEPING -> AWAKE on wake_up."""
        hb._state = "SLEEPING"
        hb.wake_up("user_message")
        assert hb._state == "AWAKE"
        assert hb._wake_reason == "user_message"

    def test_wake_already_awake_noop(self):
        """wake_up while already AWAKE does nothing."""
        hb._state = "AWAKE"
        hb.wake_up("test")
        assert hb._state == "AWAKE"
        # _wake_reason should NOT change when already awake
        assert hb._wake_reason is None

    def test_sleep_awake_to_sleeping(self):
        """AWAKE -> SLEEPING on go_to_sleep."""
        hb._state = "AWAKE"
        hb.go_to_sleep("keyword")
        assert hb._state == "SLEEPING"
        assert hb._last_sleep_at is not None

    def test_sleep_already_sleeping_noop(self):
        """go_to_sleep while already SLEEPING does nothing."""
        hb._state = "SLEEPING"
        hb.go_to_sleep("test")
        assert hb._state == "SLEEPING"
        # _last_sleep_at should remain None (never transitioned)
        assert hb._last_sleep_at is None

    def test_force_wake(self):
        """force_wake delegates to wake_up with 'user_message' reason."""
        hb._state = "SLEEPING"
        hb.force_wake()
        assert hb._state == "AWAKE"
        assert hb._wake_reason == "user_message"


class TestCheckSleepEntry:

    @pytest.fixture(autouse=True)
    def _seed_sleep_config(self):
        """Seed sleep keywords config into DB for check_sleep_entry tests."""
        from mochi.admin.admin_db import set_system_override, invalidate_system_config_cache
        set_system_override("SLEEP_KEYWORDS", "晚安,睡了")
        invalidate_system_config_cache()

    def test_keyword_in_night_window_true(self):
        """Sleep keyword during night hours returns True."""
        hb._state = "AWAKE"
        night_time = datetime(2026, 4, 13, 22, 30, tzinfo=timezone.utc)
        with patch("mochi.heartbeat.datetime") as mock_dt:
            mock_dt.now.return_value = night_time
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert hb.check_sleep_entry("晚安~") is True

    def test_keyword_outside_window_false(self):
        """Sleep keyword during daytime returns False."""
        hb._state = "AWAKE"
        day_time = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
        with patch("mochi.heartbeat.datetime") as mock_dt:
            mock_dt.now.return_value = day_time
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert hb.check_sleep_entry("晚安") is False

    def test_no_keyword_false(self):
        """Non-sleep text during night returns False."""
        hb._state = "AWAKE"
        night_time = datetime(2026, 4, 13, 23, 0, tzinfo=timezone.utc)
        with patch("mochi.heartbeat.datetime") as mock_dt:
            mock_dt.now.return_value = night_time
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert hb.check_sleep_entry("明天见") is False

    def test_sleeping_state_false(self):
        """check_sleep_entry returns False when already SLEEPING."""
        hb._state = "SLEEPING"
        assert hb.check_sleep_entry("晚安") is False

    def test_none_text_false(self):
        """check_sleep_entry returns False with None text."""
        hb._state = "AWAKE"
        assert hb.check_sleep_entry(None) is False


class TestSilentPause:

    def test_enter_silent_pause(self):
        """enter_silent_pause sets the flag."""
        assert hb._silent_pause is False
        hb.enter_silent_pause()
        assert hb._silent_pause is True

    def test_clear_silent_pause(self):
        """clear_silent_pause resets the flag."""
        hb._silent_pause = True
        hb.clear_silent_pause()
        assert hb._silent_pause is False

    def test_is_silent_pause(self):
        """is_silent_pause returns current state."""
        assert hb.is_silent_pause() is False
        hb._silent_pause = True
        assert hb.is_silent_pause() is True

    def test_enter_idempotent(self):
        """Entering silent pause twice does not error."""
        hb.enter_silent_pause()
        hb.enter_silent_pause()
        assert hb._silent_pause is True

    def test_clear_idempotent(self):
        """Clearing silent pause twice does not error."""
        hb.clear_silent_pause()
        hb.clear_silent_pause()
        assert hb._silent_pause is False


class TestGetState:

    def test_returns_current_state(self):
        """get_state returns the current state string."""
        hb._state = "AWAKE"
        assert hb.get_state() == "AWAKE"
        hb._state = "SLEEPING"
        assert hb.get_state() == "SLEEPING"

    def test_get_stats_returns_dict(self):
        """get_stats returns a dict with expected keys."""
        hb._state = "AWAKE"
        hb._proactive_count_today = 3
        stats = hb.get_stats()
        assert isinstance(stats, dict)
        assert stats["state"] == "AWAKE"
        assert stats["proactive_today"] == 3
        assert "state_changed_at" in stats
        assert "wake_reason" in stats


class TestCheckSilenceSleep:

    @pytest.fixture(autouse=True)
    def _seed_silence_config(self):
        """No DB seeding needed — check_silence_sleep reads config.py imports directly."""
        pass

    def test_not_awake_returns_none(self):
        """check_silence_sleep returns None when SLEEPING."""
        hb._state = "SLEEPING"
        assert hb.check_silence_sleep() is None

    def test_daytime_returns_none(self):
        """check_silence_sleep returns None during daytime hours."""
        hb._state = "AWAKE"
        day_time = datetime(2026, 4, 13, 15, 0, tzinfo=timezone.utc)
        with patch("mochi.heartbeat.datetime") as mock_dt:
            mock_dt.now.return_value = day_time
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            mock_dt.fromisoformat = datetime.fromisoformat
            assert hb.check_silence_sleep() is None
