"""Bounded, read-only Diary search without date rollover."""

from datetime import date

from mochi.diary import diary

MAX_ARCHIVE_FILES = 120
MAX_SCAN_BYTES = 2 * 1024 * 1024


def search_diary_entries(query: str, limit: int = 5) -> tuple[list[dict], bool]:
    needle = query.strip().casefold()
    if not needle:
        raise ValueError("query must not be empty")
    blocks: dict[str, str] = {}
    truncated = False
    remaining = MAX_SCAN_BYTES
    with diary._lock:
        paths = [diary.path] if diary.path.exists() else []
        archives = sorted(
            (diary.path.parent / "diary_archive").glob("*.md"), reverse=True,
        )
        if len(archives) > MAX_ARCHIVE_FILES:
            truncated = True
        paths.extend(archives[:MAX_ARCHIVE_FILES])
        for path in paths:
            if path.stat().st_size > remaining:
                truncated = True
                break
            with path.open("rb") as stream:
                raw_bytes = stream.read(remaining + 1)
            if len(raw_bytes) > remaining:
                truncated = True
                break
            remaining -= len(raw_bytes)
            raw = raw_bytes.decode("utf-8")
            for day, block in diary._archive_blocks(raw):
                date.fromisoformat(day)
                blocks.setdefault(day, diary._section_content(block, "今日日記"))

    results = [
        {"date": day, "content": blocks[day]}
        for day in sorted(blocks, reverse=True)
        if needle in blocks[day].casefold()
    ]
    return results[:max(1, min(limit, 10))], truncated
