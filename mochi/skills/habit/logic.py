"""Habit logic — frequency parsing and time extraction.

Pure computation: frequency parsing, day filtering, time marker extraction.
No DB, no IO, no LLM calls.
"""

import re

_FREQ_RE = re.compile(r'^(daily|weekly):(\d+)$')
_FREQ_ON_RE = re.compile(r'^weekly_on:([a-z,]+):(\d+)$')
_DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_frequency(freq: str) -> tuple[str, int] | None:
    """Parse frequency string into (cycle, target) or None.

    Supported formats:
      - "daily:N"  — N times per day
      - "weekly:N" — N times per week
      - "weekly_on:DAY,...:N" — N times per week, only on specified days
        (e.g. "weekly_on:sat,sun:1")
    """
    m = _FREQ_RE.match(freq)
    if m:
        return m.group(1), int(m.group(2))
    m = _FREQ_ON_RE.match(freq)
    if m:
        days_str, target = m.group(1), int(m.group(2))
        days = days_str.split(",")
        if all(d in _DAY_MAP for d in days) and days:
            return "weekly", int(target)
    return None


def get_allowed_days(freq: str) -> set[int] | None:
    """Extract allowed weekday numbers from weekly_on frequency (0=Mon..6=Sun).

    Returns None for daily or plain weekly (all days allowed).
    """
    m = _FREQ_ON_RE.match(freq)
    if not m:
        return None
    days = m.group(1).split(",")
    return {_DAY_MAP[d] for d in days if d in _DAY_MAP}


def extract_time_markers(context: str) -> list[int]:
    """Extract explicit HH:MM hours from habit context string.

    Only extracts numeric HH:MM patterns (e.g. "22:00", "20:00").
    Semantic time references are left to the LLM.
    Returns sorted list of unique hours.
    """
    if not context:
        return []
    hours: list[int] = []
    for m in re.finditer(r'(\d{1,2}):(\d{2})', context):
        h = int(m.group(1))
        if 0 <= h <= 23:
            hours.append(h)
    return sorted(set(hours))


# Patterns that indicate a daily habit is split morning/evening
_MORNING_EVENING_RE = re.compile(r'早晚|早.*晚|morning.*evening|am.*pm', re.IGNORECASE)

# Default split: morning window ends at 14:00, evening window starts at 17:00
_MORNING_END = 14
_EVENING_START = 17


def next_dose_due(context: str, target: int, done: int, hour_now: int) -> bool:
    """For daily:N habits with N>1, check if the next dose is due now.

    Returns True if it's time to nudge about the next incomplete dose.
    Returns True by default if no time-distribution pattern is detected.

    For morning/evening habits with target=2:
      - done=0 -> always due (morning dose missed)
      - done=1 and hour < EVENING_START -> not due yet (evening dose is later)
      - done=1 and hour >= EVENING_START -> due (evening dose time)
    """
    if target <= 1 or done >= target:
        return done < target

    # Only apply smart scheduling for recognized patterns
    if not _MORNING_EVENING_RE.search(context or ""):
        return True

    if target == 2:
        if done == 0:
            return True
        # done == 1: morning done, evening not yet
        return hour_now >= _EVENING_START

    # For target > 2 with morning/evening context, use simple even distribution
    hours_per_slot = 16 / target  # ~16 waking hours (7am-11pm)
    expected_by_now = min(target, int((hour_now - 7) / hours_per_slot) + 1) if hour_now >= 7 else 0
    return done < expected_by_now
