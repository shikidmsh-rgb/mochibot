"""Tests for the Observer plugin system."""

import asyncio
import os
import tempfile
from datetime import timedelta

import pytest

# Override DB path BEFORE importing mochi modules
_temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_temp_db.close()
os.environ["MOCHIBOT_DB_PATH"] = _temp_db.name

from mochi.observers.base import Observer, ObserverMeta, _parse_observation_md
import mochi.observers as registry_module


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

class _AlwaysObserver(Observer):
    """Test observer that always returns fixed data."""
    async def observe(self) -> dict:
        return {"value": 42}


class _EmptyObserver(Observer):
    """Test observer that always returns {}."""
    async def observe(self) -> dict:
        return {}


class _ErrorObserver(Observer):
    """Test observer that always raises."""
    async def observe(self) -> dict:
        raise RuntimeError("intentional failure")


# ═══════════════════════════════════════════════════════════════════════════
# ObserverMeta / OBSERVATION.md parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestObservationMdParsing:
    def test_parse_valid_frontmatter(self, tmp_path):
        md = tmp_path / "OBSERVATION.md"
        md.write_text(
            "---\n"
            "name: test_obs\n"
            "interval: 45\n"
            "enabled: true\n"
            "requires_config: [FOO_KEY, BAR_KEY]\n"
            "---\n\n"
            "Some description.\n"
        )
        meta = _parse_observation_md(str(md))
        assert meta.name == "test_obs"
        assert meta.interval == 45
        assert meta.enabled is True
        assert "FOO_KEY" in meta.requires_config
        assert "BAR_KEY" in meta.requires_config

    def test_parse_disabled(self, tmp_path):
        md = tmp_path / "OBSERVATION.md"
        md.write_text("---\nname: off\nenabled: false\n---\n")
        meta = _parse_observation_md(str(md))
        assert meta.enabled is False

    def test_parse_missing_file(self, tmp_path):
        meta = _parse_observation_md(str(tmp_path / "nonexistent.md"))
        # Defaults
        assert meta.interval == 20
        assert meta.enabled is True
        assert meta.requires_config == []

    def test_parse_bad_interval_falls_back(self, tmp_path):
        md = tmp_path / "OBSERVATION.md"
        md.write_text("---\ninterval: notanumber\n---\n")
        meta = _parse_observation_md(str(md))
        assert meta.interval == 20  # default


# ═══════════════════════════════════════════════════════════════════════════
# Observer base: interval / caching / error handling
# ═══════════════════════════════════════════════════════════════════════════

class TestObserverBase:
    def test_should_collect_on_first_run(self):
        obs = _AlwaysObserver()
        from datetime import datetime, timezone
        assert obs.should_collect(datetime.now(timezone.utc)) is True

    def test_should_collect_respects_interval(self):
        obs = _AlwaysObserver()
        obs._meta = ObserverMeta(name="test", interval=60)
        from datetime import datetime, timezone, timedelta
        obs._last_collected_at = datetime.now(timezone.utc)
        # Just collected — should NOT collect again immediately
        assert obs.should_collect(datetime.now(timezone.utc)) is False
        # 61 minutes later — should collect
        future = datetime.now(timezone.utc) + timedelta(minutes=61)
        assert obs.should_collect(future) is True

    def test_safe_observe_returns_data(self):
        obs = _AlwaysObserver()
        obs._meta = ObserverMeta(name="always", interval=0)
        data = asyncio.run(obs.safe_observe())
        assert data == {"value": 42}

    def test_safe_observe_caches_on_no_interval(self):
        obs = _AlwaysObserver()
        obs._meta = ObserverMeta(name="always", interval=9999)
        from datetime import datetime, timezone
        obs._last_collected_at = datetime.now(timezone.utc)
        obs._last_data = {"cached": True}
        data = asyncio.run(obs.safe_observe())
        assert data == {"cached": True}

    def test_safe_observe_handles_error_silently(self):
        obs = _ErrorObserver()
        obs._meta = ObserverMeta(name="error_obs", interval=0)
        # Should NOT raise
        data = asyncio.run(obs.safe_observe())
        assert data == {}  # empty stale cache
        assert obs._consecutive_errors == 1

    def test_safe_observe_accumulates_consecutive_errors(self):
        obs = _ErrorObserver()
        obs._meta = ObserverMeta(name="err", interval=0)
        for _ in range(3):
            obs._last_collected_at = None  # force re-attempt each time
            asyncio.run(obs.safe_observe())
        assert obs._consecutive_errors == 3

    def test_safe_observe_resets_error_counter_on_success(self):
        obs = _AlwaysObserver()
        obs._meta = ObserverMeta(name="ok", interval=0)
        obs._consecutive_errors = 4
        obs._last_collected_at = None
        asyncio.run(obs.safe_observe())
        assert obs._consecutive_errors == 0


# ═══════════════════════════════════════════════════════════════════════════
# Registry: collect_all
# ═══════════════════════════════════════════════════════════════════════════

class TestRegistry:
    def setup_method(self):
        """Reset registry before each test."""
        registry_module._observers.clear()

    def teardown_method(self):
        registry_module._observers.clear()

    def test_collect_all_empty(self):
        result = asyncio.run(registry_module.collect_all())
        assert result == {}

    def test_collect_all_includes_data(self):
        obs = _AlwaysObserver()
        obs._meta = ObserverMeta(name="always", interval=0)
        registry_module._observers["always"] = obs

        result = asyncio.run(registry_module.collect_all())
        assert "always" in result
        assert result["always"] == {"value": 42}

    def test_collect_all_omits_empty(self):
        obs = _EmptyObserver()
        obs._meta = ObserverMeta(name="empty", interval=0)
        registry_module._observers["empty"] = obs

        result = asyncio.run(registry_module.collect_all())
        assert "empty" not in result

    def test_collect_all_skips_disabled(self):
        obs = _AlwaysObserver()
        obs._meta = ObserverMeta(name="disabled", interval=0, enabled=False)
        registry_module._observers["disabled"] = obs

        result = asyncio.run(registry_module.collect_all())
        assert result == {}

    def test_collect_all_skips_5_consecutive_errors(self):
        obs = _AlwaysObserver()
        obs._meta = ObserverMeta(name="burned", interval=0)
        obs._consecutive_errors = 5
        registry_module._observers["burned"] = obs

        result = asyncio.run(registry_module.collect_all())
        assert result == {}

    def test_collect_all_survives_one_error(self):
        good = _AlwaysObserver()
        good._meta = ObserverMeta(name="good", interval=0)
        bad = _ErrorObserver()
        bad._meta = ObserverMeta(name="bad", interval=0)

        registry_module._observers["good"] = good
        registry_module._observers["bad"] = bad

        result = asyncio.run(registry_module.collect_all())
        assert "good" in result
        assert "bad" not in result  # silently dropped

    def test_list_observers(self):
        obs = _AlwaysObserver()
        obs._meta = ObserverMeta(name="always", interval=30)
        registry_module._observers["always"] = obs

        listing = registry_module.list_observers()
        assert len(listing) == 1
        assert listing[0]["name"] == "always"
        assert listing[0]["interval"] == 30


# ═══════════════════════════════════════════════════════════════════════════
# Habit DB helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestHabitDb:
    @pytest.fixture(autouse=True)
    def fresh_db(self, tmp_path, monkeypatch):
        import mochi.db as db_module
        from mochi.db import init_db
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
        init_db()

    def test_create_and_overview(self):
        from mochi.db import create_habit, get_habits_overview
        create_habit(1, "meditation", "Daily sit")
        habits = get_habits_overview(1)
        assert len(habits) == 1
        assert habits[0]["name"] == "meditation"
        assert habits[0]["logged_today"] is False
        assert habits[0]["streak_days"] == 0

    def test_log_habit_today(self):
        from mochi.db import create_habit, log_habit, get_habits_overview
        create_habit(1, "exercise")
        ok = log_habit(1, "exercise")
        assert ok is True
        habits = get_habits_overview(1)
        assert habits[0]["logged_today"] is True
        assert habits[0]["streak_days"] == 1

    def test_log_unknown_habit_returns_false(self):
        from mochi.db import log_habit
        assert log_habit(1, "nonexistent") is False

    def test_streak_accumulates(self):
        from datetime import datetime, timezone, timedelta
        from mochi.db import create_habit, get_habits_overview
        import mochi.db as db_mod

        create_habit(1, "running")
        conn = db_mod._connect()
        # Manually insert logs for past 3 days
        habit_id = conn.execute(
            "SELECT id FROM habits WHERE user_id=1 AND name='running'"
        ).fetchone()["id"]
        now = datetime.now(timezone(timedelta(hours=8)))
        for days_ago in range(3):
            day = (now - timedelta(days=days_ago)).isoformat()
            conn.execute(
                "INSERT INTO habit_logs (habit_id, user_id, logged_at) VALUES (?,?,?)",
                (habit_id, 1, day),
            )
        conn.commit()
        conn.close()

        habits = get_habits_overview(1)
        assert habits[0]["streak_days"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# time_context observer
# ═══════════════════════════════════════════════════════════════════════════

class TestTimeContextObserver:
    """Tests for the pure-code time context observer."""

    def test_basic_fields_present(self, monkeypatch):
        """Observer returns expected keys on a normal run."""
        monkeypatch.setenv("OWNER_USER_ID", "0")
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "OWNER_USER_ID", 0)

        from mochi.observers.time_context.observer import TimeContextObserver
        obs = TimeContextObserver()
        obs._meta = ObserverMeta(name="time_context", interval=0)
        data = asyncio.run(obs.safe_observe())

        assert "date" in data
        assert "weekday" in data
        assert "hour" in data
        assert "time_of_day" in data
        assert "is_weekend" in data
        assert "is_holiday" in data

    def test_time_of_day_labels(self):
        """time_of_day_label returns correct strings for each hour range."""
        from mochi.observers.time_context.observer import _time_of_day_label
        assert _time_of_day_label(6) == "early_morning"
        assert _time_of_day_label(10) == "morning"
        assert _time_of_day_label(13) == "lunch"
        assert _time_of_day_label(16) == "afternoon"
        assert _time_of_day_label(19) == "evening"
        assert _time_of_day_label(22) == "night"
        assert _time_of_day_label(2) == "late_night"

    def test_holiday_detection_christmas(self):
        from mochi.observers.time_context.observer import _is_holiday
        from datetime import datetime, timezone
        christmas = datetime(2026, 12, 25, 12, 0, tzinfo=timezone.utc)
        is_hol, name = _is_holiday(christmas)
        assert is_hol is True
        assert "Christmas" in name

    def test_non_holiday(self):
        from mochi.observers.time_context.observer import _is_holiday
        from datetime import datetime, timezone
        normal_day = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        is_hol, name = _is_holiday(normal_day)
        assert is_hol is False
        assert name == ""

    def test_weekend_flag(self, monkeypatch):
        """is_weekend should reflect the actual weekday."""
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "OWNER_USER_ID", 0)

        from mochi.observers.time_context.observer import TimeContextObserver
        from datetime import datetime, timezone, timedelta

        obs = TimeContextObserver()
        obs._meta = ObserverMeta(name="time_context", interval=0)
        # Manually call observe() synchronously
        data = asyncio.run(obs.observe())

        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=0)))
        assert data["is_weekend"] == (now.weekday() >= 5)


# ═══════════════════════════════════════════════════════════════════════════
# activity_pattern observer
# ═══════════════════════════════════════════════════════════════════════════

class TestActivityPatternObserver:
    """Tests for conversation pattern detection."""

    @pytest.fixture(autouse=True)
    def fresh_db(self, tmp_path, monkeypatch):
        import mochi.db as db_module
        from mochi.db import init_db
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
        init_db()

    def _set_owner(self, monkeypatch, uid=1):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "OWNER_USER_ID", uid)

    def _insert_messages(self, uid, day_str, count):
        """Insert `count` fake user messages on the given YYYY-MM-DD date."""
        import mochi.db as db_module
        conn = db_module._connect()
        for i in range(count):
            ts = f"{day_str}T10:{i:02d}:00"
            conn.execute(
                "INSERT INTO messages (user_id, role, content, created_at) VALUES (?,?,?,?)",
                (uid, "user", f"msg {i}", ts),
            )
        conn.commit()
        conn.close()

    def test_no_owner_returns_empty(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "OWNER_USER_ID", None)
        from mochi.observers.activity_pattern.observer import ActivityPatternObserver
        obs = ActivityPatternObserver()
        data = asyncio.run(obs.observe())
        assert data == {}

    def test_no_messages_still_returns_data(self, monkeypatch):
        self._set_owner(monkeypatch, 1)
        from mochi.observers.activity_pattern.observer import ActivityPatternObserver
        obs = ActivityPatternObserver()
        data = asyncio.run(obs.observe())
        assert "today_messages" in data
        assert data["today_messages"] == 0

    def test_signals_silent_after_active_day(self, monkeypatch):
        """60 msgs yesterday, 0 today → silent_after_active_day signal."""
        self._set_owner(monkeypatch, 1)
        from datetime import datetime, timezone, timedelta
        from mochi.config import TIMEZONE_OFFSET_HOURS
        TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))
        now = datetime.now(TZ)
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        self._insert_messages(1, yesterday, 20)

        from mochi.observers.activity_pattern.observer import ActivityPatternObserver
        obs = ActivityPatternObserver()
        data = asyncio.run(obs.observe())
        assert "signals" in data
        assert "silent_after_active_day" in data["signals"]

    def test_weekly_trend_has_7_entries(self, monkeypatch):
        self._set_owner(monkeypatch, 1)
        from mochi.observers.activity_pattern.observer import ActivityPatternObserver
        obs = ActivityPatternObserver()
        data = asyncio.run(obs.observe())
        assert len(data["weekly_trend"]) == 7

    def test_no_signals_on_normal_activity(self, monkeypatch):
        """Consistent daily activity → no anomaly signals."""
        self._set_owner(monkeypatch, 1)
        from datetime import datetime, timezone, timedelta
        from mochi.config import TIMEZONE_OFFSET_HOURS
        TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))
        now = datetime.now(TZ)
        # Insert 10 messages every day for past 7 days including today
        for i in range(7):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            self._insert_messages(1, day, 10)

        from mochi.observers.activity_pattern.observer import ActivityPatternObserver
        obs = ActivityPatternObserver()
        data = asyncio.run(obs.observe())
        # No anomaly expected
        assert "signals" not in data or "unusually_quiet" not in data.get("signals", [])


# ═══════════════════════════════════════════════════════════════════════════
# DB: get_daily_message_counts
# ═══════════════════════════════════════════════════════════════════════════

class TestDailyMessageCounts:
    @pytest.fixture(autouse=True)
    def fresh_db(self, tmp_path, monkeypatch):
        import mochi.db as db_module
        from mochi.db import init_db
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
        init_db()

    def test_returns_n_days(self):
        from mochi.db import get_daily_message_counts
        result = get_daily_message_counts(1, days=7)
        assert len(result) == 7

    def test_zero_fill_for_silent_days(self):
        from mochi.db import get_daily_message_counts
        result = get_daily_message_counts(1, days=3)
        assert all(d["count"] == 0 for d in result)

    def test_counts_correct_day(self):
        from mochi.db import get_daily_message_counts, save_message
        save_message(1, "user", "hello")
        result = get_daily_message_counts(1, days=7)
        today = result[-1]  # last entry is today
        assert today["count"] >= 1


# ═══════════════════════════════════════════════════════════════════════════
# recent_conversation observer
# ═══════════════════════════════════════════════════════════════════════════

class TestRecentConversationObserver:
    """Tests for the conversation history observer."""

    @pytest.fixture(autouse=True)
    def fresh_db(self, tmp_path, monkeypatch):
        import mochi.db as db_module
        from mochi.db import init_db
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
        init_db()

    def _set_owner(self, monkeypatch, uid=1):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "OWNER_USER_ID", uid)

    def test_no_owner_returns_empty(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "OWNER_USER_ID", None)
        from mochi.observers.recent_conversation.observer import RecentConversationObserver
        obs = RecentConversationObserver()
        data = asyncio.run(obs.observe())
        assert data == {}

    def test_no_messages_returns_empty(self, monkeypatch):
        self._set_owner(monkeypatch, 1)
        from mochi.observers.recent_conversation.observer import RecentConversationObserver
        obs = RecentConversationObserver()
        data = asyncio.run(obs.observe())
        assert data == {}

    def test_returns_messages_and_count(self, monkeypatch):
        self._set_owner(monkeypatch, 1)
        from mochi.db import save_message
        save_message(1, "user", "Hello there")
        save_message(1, "assistant", "Hi! How are you?")

        from mochi.observers.recent_conversation.observer import RecentConversationObserver
        obs = RecentConversationObserver()
        data = asyncio.run(obs.observe())

        assert "messages" in data
        assert data["count"] == 2
        roles = [m["role"] for m in data["messages"]]
        assert "user" in roles
        assert "assistant" in roles

    def test_last_user_message_shortcut(self, monkeypatch):
        self._set_owner(monkeypatch, 1)
        from mochi.db import save_message
        save_message(1, "user", "I am feeling stressed today")
        save_message(1, "assistant", "Tell me more")

        from mochi.observers.recent_conversation.observer import RecentConversationObserver
        obs = RecentConversationObserver()
        data = asyncio.run(obs.observe())

        assert "last_user_message" in data
        assert "stressed" in data["last_user_message"]
        assert "last_user_message_when" in data

    def test_long_message_is_truncated(self, monkeypatch):
        self._set_owner(monkeypatch, 1)
        from mochi.db import save_message
        long_text = "x" * 500
        save_message(1, "user", long_text)

        from mochi.observers.recent_conversation.observer import (
            RecentConversationObserver, MAX_CHARS_PER_MSG,
        )
        obs = RecentConversationObserver()
        data = asyncio.run(obs.observe())

        msg_content = data["messages"][0]["content"]
        assert len(msg_content) <= MAX_CHARS_PER_MSG + 1  # +1 for "…"
        assert msg_content.endswith("…")

    def test_message_order_oldest_first(self, monkeypatch):
        self._set_owner(monkeypatch, 1)
        from mochi.db import save_message
        save_message(1, "user", "first message")
        save_message(1, "assistant", "second message")
        save_message(1, "user", "third message")

        from mochi.observers.recent_conversation.observer import RecentConversationObserver
        obs = RecentConversationObserver()
        data = asyncio.run(obs.observe())

        assert data["messages"][0]["content"] == "first message"
        assert data["messages"][-1]["content"] == "third message"

    def test_relative_time_labels(self):
        from mochi.observers.recent_conversation.observer import _relative_time
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        assert _relative_time(now.isoformat(), now) == "just now"

        past_30m = (now - timedelta(minutes=30)).isoformat()
        assert "30m ago" in _relative_time(past_30m, now)

        past_3h = (now - timedelta(hours=3)).isoformat()
        assert "3h ago" in _relative_time(past_3h, now)

        past_2d = (now - timedelta(days=2)).isoformat()
        assert "2d ago" in _relative_time(past_2d, now)
