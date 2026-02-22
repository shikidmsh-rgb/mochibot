"""Unit tests for mochi.skills.habit.logic — pure functions, no DB needed."""

import pytest
from mochi.skills.habit.logic import (
    parse_frequency,
    get_allowed_days,
    extract_time_markers,
    next_dose_due,
)


# ── parse_frequency ──────────────────────────────────────────────────────

class TestParseFrequency:
    def test_daily(self):
        assert parse_frequency("daily:1") == ("daily", 1)
        assert parse_frequency("daily:2") == ("daily", 2)
        assert parse_frequency("daily:10") == ("daily", 10)

    def test_weekly(self):
        assert parse_frequency("weekly:3") == ("weekly", 3)
        assert parse_frequency("weekly:1") == ("weekly", 1)

    def test_weekly_on(self):
        assert parse_frequency("weekly_on:sat,sun:1") == ("weekly", 1)
        assert parse_frequency("weekly_on:mon,wed,fri:2") == ("weekly", 2)

    def test_invalid(self):
        assert parse_frequency("") is None
        assert parse_frequency("monthly:1") is None
        assert parse_frequency("daily") is None
        assert parse_frequency("daily:0") == ("daily", 0)
        assert parse_frequency("weekly_on:foo:1") is None
        assert parse_frequency("weekly_on::1") is None


# ── get_allowed_days ─────────────────────────────────────────────────────

class TestGetAllowedDays:
    def test_weekly_on(self):
        assert get_allowed_days("weekly_on:sat,sun:1") == {5, 6}
        assert get_allowed_days("weekly_on:mon:1") == {0}
        assert get_allowed_days("weekly_on:mon,wed,fri:2") == {0, 2, 4}

    def test_non_weekly_on(self):
        assert get_allowed_days("daily:1") is None
        assert get_allowed_days("weekly:3") is None

    def test_invalid_days(self):
        # "foo" is not a valid day, but format matches — returns empty set
        # (parse_frequency rejects this entirely, so get_allowed_days won't
        #  be called in practice)
        assert get_allowed_days("weekly_on:foo:1") == set()


# ── extract_time_markers ─────────────────────────────────────────────────

class TestExtractTimeMarkers:
    def test_single_time(self):
        assert extract_time_markers("22:00") == [22]

    def test_multiple_times(self):
        assert extract_time_markers("morning 8:00, evening 20:00") == [8, 20]

    def test_no_times(self):
        assert extract_time_markers("morning and evening") == []
        assert extract_time_markers("") == []

    def test_dedup(self):
        assert extract_time_markers("8:00 and 8:00") == [8]

    def test_invalid_hour(self):
        assert extract_time_markers("25:00") == []

    def test_none_input(self):
        assert extract_time_markers(None) == []


# ── next_dose_due ────────────────────────────────────────────────────────

class TestNextDoseDue:
    def test_single_target(self):
        assert next_dose_due("", 1, 0, 10) is True
        assert next_dose_due("", 1, 1, 10) is False

    def test_already_done(self):
        assert next_dose_due("morning and evening", 2, 2, 10) is False

    def test_morning_evening_done0(self):
        # Morning dose not done — always due
        assert next_dose_due("morning and evening", 2, 0, 8) is True
        assert next_dose_due("morning and evening", 2, 0, 20) is True

    def test_morning_evening_done1_before_evening(self):
        # Morning done, evening not yet, before evening window
        assert next_dose_due("morning and evening", 2, 1, 10) is False

    def test_morning_evening_done1_evening_time(self):
        # Morning done, evening time
        assert next_dose_due("morning and evening", 2, 1, 17) is True
        assert next_dose_due("morning and evening", 2, 1, 20) is True

    def test_no_pattern_always_due(self):
        # No recognized pattern — always due if incomplete
        assert next_dose_due("random context", 2, 1, 10) is True

    def test_chinese_pattern(self):
        assert next_dose_due("早晚各一次", 2, 1, 10) is False
        assert next_dose_due("早晚各一次", 2, 1, 18) is True
