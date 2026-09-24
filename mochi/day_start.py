"""Once-per-day look back across recent diaries, plus the near calendar."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

DAY_START_JOB = "day_start"
DAY_START_DIARY_DAYS = 7
DAY_START_DIARY_MAX_CHARS = 12_000
CALENDAR_HORIZON_DAYS = 14

_HOLIDAY_NAMES = {
    "New Year's Day": "元旦",
    "Spring Festival": "春节",
    "Tomb-sweeping Day": "清明节",
    "Labour Day": "劳动节",
    "Dragon Boat Festival": "端午节",
    "National Day": "国庆节",
    "Mid-autumn Festival": "中秋节",
}
_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def pending_day(now: datetime) -> str | None:
    """Logical day whose day-start look Main has not yet completed."""
    from mochi.config import logical_today
    from mochi.db import get_scheduled_run
    from mochi.heartbeat import _is_awake_hour

    if not _is_awake_hour(now.hour):
        return None
    day = logical_today(now)
    run = get_scheduled_run(DAY_START_JOB, day)
    return None if run and run["status"] == "success" else day


def context(day: str) -> str:
    """Render the day-start block, or "" when no archived diary exists."""
    from mochi.diary import read_diary_archive_window
    from mochi.prompt_loader import get_prompt

    end = date.fromisoformat(day)
    window = read_diary_archive_window(
        end - timedelta(days=DAY_START_DIARY_DAYS), end,
        max_chars=DAY_START_DIARY_MAX_CHARS, keep_newest=True,
    )
    if not window.content:
        return ""
    template = get_prompt("day_start_context")
    if not template:
        raise RuntimeError("Day start prompt is missing")
    return template.replace("{{recent_diary}}", window.content)


def mark_done(day: str) -> None:
    from mochi.db import claim_scheduled_run, finish_scheduled_run

    if claim_scheduled_run(DAY_START_JOB, day):
        finish_scheduled_run(DAY_START_JOB, day, success=True)
        log.info("Day start look completed for %s", day)


def calendar_horizon(now: datetime) -> str:
    """Mainland holidays and weekend make-up workdays in the coming days."""
    if now.utcoffset() != timedelta(hours=8):
        return ""
    import chinese_calendar as cc

    start = now.date()
    holiday_runs: list[list] = []
    makeup_days: list[date] = []
    for offset in range(CALENDAR_HORIZON_DAYS):
        day = start + timedelta(days=offset)
        try:
            is_holiday, name = cc.get_holiday_detail(day)
            is_workday = cc.is_workday(day)
        except NotImplementedError:
            break
        if is_holiday and name:
            label = _HOLIDAY_NAMES.get(name, name)
            last = holiday_runs[-1] if holiday_runs else None
            if last and last[0] == label and last[2] == day - timedelta(days=1):
                last[2] = day
            else:
                holiday_runs.append([label, day, day])
        elif is_workday and day.weekday() >= 5:
            makeup_days.append(day)

    items = [
        (
            first,
            f"{first:%m-%d}~{last:%m-%d} {label}假期"
            if last != first
            else f"{first:%m-%d} {label}假期",
        )
        for label, first, last in holiday_runs
    ]
    items.extend(
        (day, f"{day:%m-%d}({_WEEKDAY_LABELS[day.weekday()]})调休上班")
        for day in makeup_days
    )
    return "；".join(text for _, text in sorted(items))
