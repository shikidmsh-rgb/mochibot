"""Product-level companion preferences shown in the admin portal."""

from __future__ import annotations

from datetime import time

from mochi.heartbeat_runtime import (
    format_clock_time,
    free_time_clock_capacity,
    try_parse_clock_time,
)


PREFERENCE_KEYS = (
    "TIMEZONE_OFFSET_HOURS",
    "MAX_DAILY_FREE_TIME",
    "SLEEP_AFTER_HOUR",
    "WAKE_EARLIEST_HOUR",
    "FREE_TIME_AWAKE_START",
    "FREE_TIME_AWAKE_END",
)

_HOUR_KEYS = frozenset({"SLEEP_AFTER_HOUR", "WAKE_EARLIEST_HOUR"})
_CLOCK_KEYS = frozenset({"FREE_TIME_AWAKE_START", "FREE_TIME_AWAKE_END"})


def _as_hour(key: str, raw_value: object) -> int:
    if isinstance(raw_value, bool):
        raise ValueError(f"{key} must be an hour between 0 and 23")
    if isinstance(raw_value, str):
        text = raw_value.strip()
        parsed = try_parse_clock_time(text)
        if parsed is not None:
            if parsed.minute != 0:
                raise ValueError(f"{key} only accepts whole hours")
            return parsed.hour
        try:
            raw_value = float(text) if "." in text else int(text)
        except ValueError as exc:
            raise ValueError(f"{key} must be an hour between 0 and 23") from exc
    try:
        if isinstance(raw_value, float):
            if not raw_value.is_integer():
                raise ValueError
            hour = int(raw_value)
        else:
            hour = int(raw_value)  # type: ignore[arg-type]
            if isinstance(raw_value, str) and str(hour) != str(raw_value).strip():
                raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an hour between 0 and 23") from exc
    if not 0 <= hour <= 23:
        raise ValueError(f"{key} must be an hour between 0 and 23")
    return hour


def _as_clock(key: str, raw_value: object) -> str:
    parsed = try_parse_clock_time(raw_value)
    if parsed is None:
        raise ValueError(f"{key} must be a clock time in HH:MM")
    return format_clock_time(parsed)


def _as_timezone(raw_value: object) -> float:
    if isinstance(raw_value, bool):
        raise ValueError("TIMEZONE_OFFSET_HOURS must be a number")
    try:
        value = float(raw_value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("TIMEZONE_OFFSET_HOURS must be a number") from exc
    if not -12.0 <= value <= 14.0:
        raise ValueError("TIMEZONE_OFFSET_HOURS must be between -12 and 14")
    return value


def _as_daily_count(raw_value: object, maximum: int) -> int:
    if isinstance(raw_value, bool):
        raise ValueError("MAX_DAILY_FREE_TIME must be a number")
    try:
        if isinstance(raw_value, str):
            text = raw_value.strip()
            value = int(text)
            if str(value) != text:
                raise ValueError
        elif isinstance(raw_value, float):
            if not raw_value.is_integer():
                raise ValueError
            value = int(raw_value)
        else:
            value = int(raw_value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("MAX_DAILY_FREE_TIME must be a number") from exc
    if not 0 <= value <= maximum:
        raise ValueError(
            f"MAX_DAILY_FREE_TIME must be between 0 and {maximum}"
        )
    return value


def _clock_from_current(value: object, fallback: time) -> time:
    parsed = try_parse_clock_time(value)
    return parsed if parsed is not None else fallback


def resolve_free_time_capacity(merged: dict[str, object]) -> int:
    from mochi.heartbeat_runtime import FREE_TIME_AWAKE_END, FREE_TIME_AWAKE_START

    start = _clock_from_current(
        merged.get("FREE_TIME_AWAKE_START"), FREE_TIME_AWAKE_START,
    )
    end = _clock_from_current(
        merged.get("FREE_TIME_AWAKE_END"), FREE_TIME_AWAKE_END,
    )
    if start == end:
        raise ValueError("FREE_TIME_AWAKE_START and FREE_TIME_AWAKE_END must differ")
    return free_time_clock_capacity(start, end)


def normalize_preference_updates(
    body: dict,
    current: dict[str, object],
) -> dict[str, str]:
    """Validate a PUT body and return string values for system config."""
    if not isinstance(body, dict) or not body:
        raise ValueError("preferences must be a non-empty object")
    unknown = sorted(set(body) - set(PREFERENCE_KEYS))
    if unknown:
        raise ValueError(f"Unknown preference: {', '.join(unknown)}")

    merged = dict(current)
    normalized: dict[str, str] = {}
    for key, raw_value in body.items():
        if key == "TIMEZONE_OFFSET_HOURS":
            value: object = _as_timezone(raw_value)
        elif key in _HOUR_KEYS:
            value = _as_hour(key, raw_value)
        elif key in _CLOCK_KEYS:
            value = _as_clock(key, raw_value)
        elif key == "MAX_DAILY_FREE_TIME":
            merged[key] = raw_value
            continue
        else:
            raise ValueError(f"Unknown preference: {key}")
        merged[key] = value
        normalized[key] = str(value)

    sleep_after = _as_hour(
        "SLEEP_AFTER_HOUR", merged.get("SLEEP_AFTER_HOUR", 1),
    )
    wake_earliest = _as_hour(
        "WAKE_EARLIEST_HOUR", merged.get("WAKE_EARLIEST_HOUR", 8),
    )
    if sleep_after == wake_earliest:
        raise ValueError(
            "SLEEP_AFTER_HOUR and WAKE_EARLIEST_HOUR must differ"
        )

    capacity = resolve_free_time_capacity(merged)
    if "MAX_DAILY_FREE_TIME" in body:
        normalized["MAX_DAILY_FREE_TIME"] = str(
            _as_daily_count(body["MAX_DAILY_FREE_TIME"], capacity)
        )
    return normalized
