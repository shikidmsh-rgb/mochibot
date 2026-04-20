"""Lint test: ensure wall-clock date constructions in mochi/ are intentional.

Any `datetime.now(TZ).strftime("%Y-%m-%d")` (and a couple of equivalent
patterns) must be annotated with `# wall-clock 故意` on the same or previous
line, OR live in a known external/physical-time site.

This is the durable defense against the logical_today / wall-clock mixup bug.
If you add a new wall-clock date site, either annotate it, or convert it to
`logical_today()` / `logical_days_ago(n)` from `mochi.config`.
"""

import re
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MOCHI_DIR = _PROJECT_ROOT / "mochi"

# Patterns that build a YYYY-MM-DD string from wall-clock now.
# Multiple patterns because the codebase varies between `now.strftime(...)` and
# `datetime.now(TZ).strftime(...)` and back-walks like `(now - timedelta(...)).strftime("%Y-%m-%d")`.
_PATTERNS = [
    re.compile(r'datetime\.now\([^)]*\)\.strftime\(["\']%Y-%m-%d["\']\)'),
    re.compile(r'\(now\s*[-+]\s*timedelta\([^)]*\)\)\.strftime\(["\']%Y-%m-%d["\']\)'),
    re.compile(r'\(datetime\.now\([^)]*\)\s*[-+]\s*timedelta\([^)]*\)\)\.strftime\(["\']%Y-%m-%d["\']\)'),
    re.compile(r'\bnow\.strftime\(["\']%Y-%m-%d["\']\)'),
]

_MARKER = "wall-clock 故意"

# Files where wall-clock date construction is the implementation itself
# (logical_today, logical_yesterday, logical_days_ago all live here).
_INTERNAL_WHITELIST = {
    "mochi/config.py",
}


def _gather_hits():
    """Yield (path, lineno, line) for every wall-clock date construction in mochi/."""
    for py in _MOCHI_DIR.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, start=1):
            for pat in _PATTERNS:
                if pat.search(line):
                    yield py, i, line, lines
                    break


def test_wallclock_date_sites_are_annotated():
    """Every wall-clock YYYY-MM-DD construction must carry the marker comment."""
    unannotated = []
    for path, lineno, line, lines in _gather_hits():
        rel = path.relative_to(_PROJECT_ROOT).as_posix()
        if rel in _INTERNAL_WHITELIST:
            continue
        prev_line = lines[lineno - 2] if lineno >= 2 else ""
        if _MARKER in line or _MARKER in prev_line:
            continue
        unannotated.append(f"{rel}:{lineno}: {line.strip()}")

    assert not unannotated, (
        "Found wall-clock YYYY-MM-DD constructions without `# wall-clock 故意` marker.\n"
        "Either add the marker (with a one-line reason) or convert to logical_today() / "
        "logical_days_ago(n) from mochi.config:\n  "
        + "\n  ".join(unannotated)
    )
