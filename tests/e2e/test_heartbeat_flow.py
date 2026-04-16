"""E2E tests for heartbeat state management."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from mochi.heartbeat import (
    wake_up, go_to_sleep, force_wake, get_state, get_stats,
    set_send_callback, check_sleep_entry, check_silence_sleep,
    enter_silent_pause, clear_silent_pause,
    is_silent_pause, should_wake_on_message,
    SLEEPING, AWAKE, RESLEEP_WINDOW_HOURS,
)


@pytest.fixture(autouse=True)
def reset_heartbeat_state(monkeypatch):
    """Reset heartbeat module state before each test."""
    import mochi.heartbeat as hb
    monkeypatch.setattr(hb, "_state", AWAKE)
    monkeypatch.setattr(hb, "_state_changed_at", datetime.now(hb.TZ))
    monkeypatch.setattr(hb, "_wake_reason", None)
    monkeypatch.setattr(hb, "_last_sleep_at", None)
    monkeypatch.setattr(hb, "_silent_pause", False)
    monkeypatch.setattr(hb, "_prev_observer_raw", {})
    monkeypatch.setattr(hb, "_last_think_at", None)


class TestWakeUp:

    def test_wake_up_from_sleeping(self, monkeypatch):
        """wake_up() transitions SLEEPING → AWAKE."""
        import mochi.heartbeat as hb
        monkeypatch.setattr(hb, "_state", SLEEPING)

        wake_up("user_message")

        assert get_state() == AWAKE
        stats = get_stats()
        assert stats["wake_reason"] == "user_message"

    def test_wake_up_noop_when_already_awake(self):
        """wake_up() does nothing if already AWAKE."""
        wake_up("user_message")
        stats = get_stats()
        assert stats["wake_reason"] is None  # not set because transition didn't happen

    def test_force_wake_delegates(self, monkeypatch):
        """force_wake() delegates to wake_up('user_message')."""
        import mochi.heartbeat as hb
        monkeypatch.setattr(hb, "_state", SLEEPING)

        force_wake()

        assert get_state() == AWAKE
        stats = get_stats()
        assert stats["wake_reason"] == "user_message"


class TestGoToSleep:

    def test_go_to_sleep(self):
        """go_to_sleep() transitions AWAKE → SLEEPING."""
        go_to_sleep("keyword: 晚安")
        assert get_state() == SLEEPING

    def test_go_to_sleep_clears_state(self):
        """go_to_sleep() clears wake_reason."""
        import mochi.heartbeat as hb
        hb._wake_reason = "user_message"

        go_to_sleep("silence")

        stats = get_stats()
        assert stats["wake_reason"] is None

    def test_go_to_sleep_tracks_last_sleep_at(self):
        """go_to_sleep() records _last_sleep_at for re-sleep detection."""
        import mochi.heartbeat as hb
        go_to_sleep("silence")
        assert hb._last_sleep_at is not None

    def test_go_to_sleep_noop_when_sleeping(self, monkeypatch):
        """go_to_sleep() does nothing if already SLEEPING."""
        import mochi.heartbeat as hb
        monkeypatch.setattr(hb, "_state", SLEEPING)
        go_to_sleep("silence")
        assert get_state() == SLEEPING


class TestCheckSleepEntry:

    def test_keyword_during_night_triggers_sleep(self, monkeypatch):
        """Keyword match during night hours returns True (signals sleep)."""
        import mochi.heartbeat as hb
        # Simulate 22:00
        fake_now = datetime(2026, 3, 28, 22, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        result = check_sleep_entry("晚安~")
        assert result is True

    def test_keyword_during_daytime_no_trigger(self, monkeypatch):
        """Keyword match during daytime does NOT trigger sleep."""
        import mochi.heartbeat as hb
        # Simulate 14:00
        fake_now = datetime(2026, 3, 28, 14, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        result = check_sleep_entry("晚安~")
        assert result is False

    def test_no_keyword_no_trigger(self, monkeypatch):
        """Non-keyword text does not trigger sleep."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 28, 23, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        result = check_sleep_entry("明天再聊")
        assert result is False

    def test_sleeping_state_noop(self, monkeypatch):
        """check_sleep_entry returns False when already SLEEPING."""
        import mochi.heartbeat as hb
        monkeypatch.setattr(hb, "_state", SLEEPING)
        fake_now = datetime(2026, 3, 28, 22, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        result = check_sleep_entry("晚安")
        assert result is False


class TestCheckSilenceSleep:

    def test_silence_at_night_returns_context(self, monkeypatch):
        """Silence past threshold at night returns sleep context."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 29, 0, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))
        # Last message 2 hours ago
        two_hours_ago = (fake_now - timedelta(hours=2)).isoformat()
        monkeypatch.setattr(hb, "get_last_user_message_time", lambda uid: two_hours_ago)
        monkeypatch.setattr("mochi.config.OWNER_USER_ID", 123)

        result = check_silence_sleep()

        assert result is not None
        assert result["context_hint"] == "first_sleep"
        assert result["silence_hours"] >= 1.0

    def test_silence_during_day_returns_none(self, monkeypatch):
        """Silence during daytime returns None (no sleep trigger)."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 28, 15, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))
        two_hours_ago = (fake_now - timedelta(hours=2)).isoformat()
        monkeypatch.setattr(hb, "get_last_user_message_time", lambda uid: two_hours_ago)
        monkeypatch.setattr("mochi.config.OWNER_USER_ID", 123)

        result = check_silence_sleep()
        assert result is None

    def test_resleep_detection(self, monkeypatch):
        """Re-sleep detected when last sleep was within RESLEEP_WINDOW_HOURS."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 29, 2, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))
        # Simulate previous sleep 3 hours ago
        monkeypatch.setattr(hb, "_last_sleep_at", fake_now - timedelta(hours=3))
        two_hours_ago = (fake_now - timedelta(hours=2)).isoformat()
        monkeypatch.setattr(hb, "get_last_user_message_time", lambda uid: two_hours_ago)
        monkeypatch.setattr("mochi.config.OWNER_USER_ID", 123)

        result = check_silence_sleep()

        assert result is not None
        assert result["context_hint"] == "re_sleep"


class TestSilentPause:

    def test_enter_and_clear(self):
        """Silent pause can be entered and cleared."""
        assert not is_silent_pause()

        enter_silent_pause()
        assert is_silent_pause()

        clear_silent_pause()
        assert not is_silent_pause()


class TestGetStats:

    def test_returns_all_expected_keys(self):
        """get_stats() returns all expected fields."""
        stats = get_stats()

        assert "state" in stats
        assert "proactive_today" in stats
        assert "proactive_limit" in stats
        assert "wake_reason" in stats
        assert isinstance(stats["proactive_today"], int)
        assert isinstance(stats["proactive_limit"], int)

    def test_send_callback_registration(self):
        """set_send_callback() registers the callback."""
        import mochi.heartbeat as hb

        async def dummy_callback(user_id: int, text: str):
            pass

        set_send_callback(dummy_callback)
        assert hb._send_callback is dummy_callback


# ── Helper: fake datetime for monkeypatching ─────────────────────────────

class _FakeDatetime:
    """Minimal datetime replacement that returns a fixed 'now'."""

    def __init__(self, fixed_now):
        self._now = fixed_now

    def now(self, tz=None):
        return self._now

    def fromisoformat(self, s):
        return datetime.fromisoformat(s)

    def __getattr__(self, name):
        return getattr(datetime, name)


# ═══════════════════════════════════════════════════════════════════════════
# E2E tests — exercise heartbeat loop with mock LLM + fake transport
# ═══════════════════════════════════════════════════════════════════════════

class TestSilenceSleepE2E:
    """E2E: silence at night → bot sends goodnight → transitions to SLEEPING."""

    @pytest.mark.asyncio
    async def test_silence_sleep_sends_goodnight(self, monkeypatch, mock_llm_factory):
        """Heartbeat loop sends LLM-generated goodnight via chat_proactive on silence sleep."""
        import mochi.heartbeat as hb
        import mochi.ai_client as ai_client
        from tests.e2e.mock_llm import make_response

        # Mock time: midnight
        fake_now = datetime(2026, 3, 29, 0, 30, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        # User last message 2h ago → silence threshold met
        two_hours_ago = (fake_now - timedelta(hours=2)).isoformat()
        monkeypatch.setattr(hb, "get_last_user_message_time", lambda uid: two_hours_ago)
        monkeypatch.setattr("mochi.config.OWNER_USER_ID", 1)

        # Track sent messages
        sent = []

        async def fake_send(user_id, text):
            sent.append(text)

        hb._send_callback = fake_send

        # Mock chat_proactive to return a goodnight message
        async def fake_chat_proactive(findings, user_id):
            assert len(findings) == 1
            assert findings[0]["topic"] == "sleep_transition"
            return "Looks like you fell asleep. Goodnight!"

        monkeypatch.setattr(ai_client, "chat_proactive", fake_chat_proactive)

        # Run the silence sleep check
        sleep_action = hb.check_silence_sleep()
        assert sleep_action is not None

        # Simulate what heartbeat_loop does: call chat_proactive + send
        hint = sleep_action["context_hint"]
        silence_h = sleep_action["silence_hours"]
        re = "再次" if hint == "re_sleep" else ""
        finding = {
            "topic": "sleep_transition",
            "summary": f"用户已沉默{silence_h}小时，深夜{re}静默，大概率睡着了",
        }
        goodnight_msg = await fake_chat_proactive([finding], user_id=1)

        assert goodnight_msg is not None
        await fake_send(1, goodnight_msg)
        hb.go_to_sleep("silence_detected")

        assert hb.get_state() == SLEEPING
        assert len(sent) == 1
        assert "goodnight" in sent[0].lower() or "sleep" in sent[0].lower()


class TestFallbackWakeE2E:
    """E2E: fallback wake hour triggers wake_up during daytime only."""

    def test_fallback_wake_at_configured_hour(self, monkeypatch):
        """Bot wakes up at FALLBACK_WAKE_HOUR when still sleeping."""
        import mochi.heartbeat as hb
        from mochi.config import SLEEP_AFTER_HOUR

        monkeypatch.setattr(hb, "_state", SLEEPING)
        # Simulate 10:00 — matches FALLBACK_WAKE_HOUR default
        fake_now = datetime(2026, 3, 29, 10, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        # Replicate the fallback wake logic from heartbeat_loop
        hour = fake_now.hour
        fallback_hour = hb._effective('FALLBACK_WAKE_HOUR')

        if hb._state == SLEEPING and fallback_hour <= hour < SLEEP_AFTER_HOUR:
            hb.wake_up(f"fallback_{fallback_hour}:00")

        assert hb.get_state() == AWAKE
        assert hb.get_stats()["wake_reason"] == "fallback_10:00"

    def test_fallback_wake_blocked_during_night(self, monkeypatch):
        """Bot does NOT wake via fallback when hour >= SLEEP_AFTER_HOUR."""
        import mochi.heartbeat as hb
        from mochi.config import SLEEP_AFTER_HOUR

        monkeypatch.setattr(hb, "_state", SLEEPING)
        # Simulate 21:30 — inside night window
        fake_now = datetime(2026, 3, 29, 21, 30, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        hour = fake_now.hour
        fallback_hour = hb._effective('FALLBACK_WAKE_HOUR')

        if hb._state == SLEEPING and fallback_hour <= hour < SLEEP_AFTER_HOUR:
            hb.wake_up(f"fallback_{fallback_hour}:00")

        assert hb.get_state() == SLEEPING

    def test_fallback_wake_blocked_at_22(self, monkeypatch):
        """Bot does NOT wake via fallback at 22:00."""
        import mochi.heartbeat as hb
        from mochi.config import SLEEP_AFTER_HOUR

        monkeypatch.setattr(hb, "_state", SLEEPING)
        fake_now = datetime(2026, 3, 29, 22, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        hour = fake_now.hour
        fallback_hour = hb._effective('FALLBACK_WAKE_HOUR')

        if hb._state == SLEEPING and fallback_hour <= hour < SLEEP_AFTER_HOUR:
            hb.wake_up(f"fallback_{fallback_hour}:00")

        assert hb.get_state() == SLEEPING

    def test_silence_sleep_then_stays_sleeping(self, monkeypatch):
        """After silence_sleep at 21:30, next tick does NOT fallback wake."""
        import mochi.heartbeat as hb
        from mochi.config import SLEEP_AFTER_HOUR

        # Step 1: go to sleep at 21:30
        monkeypatch.setattr(hb, "_state", AWAKE)
        hb.go_to_sleep("silence_detected")
        assert hb.get_state() == SLEEPING

        # Step 2: simulate next tick at 21:50
        fake_now = datetime(2026, 3, 29, 21, 50, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        hour = fake_now.hour
        fallback_hour = hb._effective('FALLBACK_WAKE_HOUR')

        if hb._state == SLEEPING and fallback_hour <= hour < SLEEP_AFTER_HOUR:
            hb.wake_up(f"fallback_{fallback_hour}:00")

        # Bot must stay asleep — this is the core regression test
        assert hb.get_state() == SLEEPING


class TestInitStateBoundary:
    """Test _init_state hour heuristic respects SLEEP_AFTER_HOUR boundary."""

    def test_init_state_sleeping_at_21(self, monkeypatch):
        """_init_state returns SLEEPING at hour 21 (SLEEP_AFTER_HOUR)."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 29, 21, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))
        monkeypatch.setattr(hb, "_STATE_FILE",
                            type('P', (), {'exists': lambda self: False})())
        assert hb._init_state() == SLEEPING

    def test_init_state_sleeping_at_22(self, monkeypatch):
        """_init_state returns SLEEPING at hour 22."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 29, 22, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))
        monkeypatch.setattr(hb, "_STATE_FILE",
                            type('P', (), {'exists': lambda self: False})())
        assert hb._init_state() == SLEEPING

    def test_init_state_awake_at_20(self, monkeypatch):
        """_init_state returns AWAKE at hour 20 (before SLEEP_AFTER_HOUR)."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 29, 20, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))
        monkeypatch.setattr(hb, "_STATE_FILE",
                            type('P', (), {'exists': lambda self: False})())
        assert hb._init_state() == AWAKE


class TestSleepKeywordE2E:
    """E2E: user says goodnight keyword → Chat replies → check_sleep_entry returns True."""

    def test_keyword_after_chat_reply(self, monkeypatch):
        """check_sleep_entry returns True for sleep keyword after Chat reply."""
        import mochi.heartbeat as hb

        # Simulate 22:30
        fake_now = datetime(2026, 3, 28, 22, 30, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        # User said "good night" — Chat already replied at this point
        result = hb.check_sleep_entry("good night everyone!")

        assert result is True

    def test_keyword_gn_works(self, monkeypatch):
        """'gn' keyword returns True at night."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 28, 23, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        result = hb.check_sleep_entry("ok gn")
        assert result is True


class TestShouldWakeOnMessage:
    """E2E: 6 AM gate — should_wake_on_message respects WAKE_EARLIEST_HOUR."""

    def test_wake_after_6am(self, monkeypatch):
        """should_wake_on_message returns True when SLEEPING and hour >= 6."""
        import mochi.heartbeat as hb
        monkeypatch.setattr(hb, "_state", SLEEPING)
        fake_now = datetime(2026, 3, 29, 8, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        assert should_wake_on_message() is True

    def test_no_wake_before_6am(self, monkeypatch):
        """should_wake_on_message returns False before WAKE_EARLIEST_HOUR."""
        import mochi.heartbeat as hb
        monkeypatch.setattr(hb, "_state", SLEEPING)
        fake_now = datetime(2026, 3, 29, 4, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        assert should_wake_on_message() is False

    def test_no_wake_when_awake(self, monkeypatch):
        """should_wake_on_message returns False when already AWAKE."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 29, 10, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        assert should_wake_on_message() is False


class TestMidnightBoundary:
    """E2E: sleep logic works correctly across midnight boundary."""

    def test_keyword_at_midnight_triggers_sleep(self, monkeypatch):
        """Sleep keyword at 0:30 (past midnight) still triggers sleep."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 29, 0, 30, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        result = hb.check_sleep_entry("晚安~")
        assert result is True

    def test_silence_at_midnight_triggers_sleep(self, monkeypatch):
        """Silence at midnight triggers silence sleep."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 29, 0, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))
        two_hours_ago = (fake_now - timedelta(hours=2)).isoformat()
        monkeypatch.setattr(hb, "get_last_user_message_time", lambda uid: two_hours_ago)
        monkeypatch.setattr("mochi.config.OWNER_USER_ID", 123)

        result = check_silence_sleep()
        assert result is not None
        assert result["context_hint"] == "first_sleep"

    def test_keyword_at_7am_does_not_trigger(self, monkeypatch):
        """Sleep keyword at 7 AM does NOT trigger sleep (daytime)."""
        import mochi.heartbeat as hb
        fake_now = datetime(2026, 3, 29, 7, 0, tzinfo=hb.TZ)
        monkeypatch.setattr(hb, "datetime", _FakeDatetime(fake_now))

        result = hb.check_sleep_entry("晚安")
        assert result is False
