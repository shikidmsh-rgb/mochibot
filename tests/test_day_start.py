from datetime import date, datetime, timedelta, timezone

from mochi.day_start import calendar_horizon
from mochi.diary import diary, read_diary_archive_window


def test_calendar_horizon_merges_holidays_and_lists_makeup_workdays():
    cst = timezone(timedelta(hours=8))
    horizon = calendar_horizon(datetime(2026, 9, 27, 9, tzinfo=cst))
    assert horizon == (
        "09-27 中秋节假期；10-01~10-07 国庆节假期；10-10(周六)调休上班"
    )
    assert calendar_horizon(datetime(2026, 9, 26, 9, tzinfo=timezone.utc)) == ""


def test_archive_window_can_keep_newest_whole_days():
    archive = diary.path.parent / "diary_archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "2026-09.md").write_text("\n\n".join(
        f"# Diary 2026-09-0{day}\n\n## 今日日記\n" + f"day{day} " * 40
        for day in (1, 2, 3)
    ), encoding="utf-8")

    window = read_diary_archive_window(
        date(2026, 9, 1), date(2026, 9, 4), max_chars=600, keep_newest=True,
    )

    assert window.truncated and window.dates == ("2026-09-02", "2026-09-03")
    assert "day1" not in window.content and window.content.rstrip().endswith("day3")
