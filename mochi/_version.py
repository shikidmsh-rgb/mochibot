"""Read __version__ from mochi/__init__.py without triggering import cache.

Why a file read instead of `from mochi import __version__`?
After `git pull` updates __init__.py, a running process still has the old
value cached in sys.modules. Reading the file each call ensures the
admin portal and diagnostics report always show the version on disk.
"""
from pathlib import Path
import re

_INIT_PATH = Path(__file__).parent / "__init__.py"
_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')


def read_version() -> str:
    try:
        text = _INIT_PATH.read_text(encoding="utf-8")
        m = _VERSION_RE.search(text)
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        from mochi import __version__
        return __version__
    except Exception:
        return "unknown"
