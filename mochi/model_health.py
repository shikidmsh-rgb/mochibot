"""In-memory model health tracking per tier (lite / chat / deep).

Thread-safe counters for success/failure rates.  Mirrors the
error_buffer.py pattern: pure utility, no upward imports.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

WARN_THRESHOLD = 3  # consecutive failures before user-facing warning

_lock = threading.Lock()
_stats: dict[str, dict] = {}


def _ensure(tier: str) -> dict:
    if tier not in _stats:
        _stats[tier] = {
            "total": 0,
            "failures": 0,
            "consecutive_failures": 0,
            "last_error": None,
            "last_error_time": None,
        }
    return _stats[tier]


def record_success(tier: str) -> None:
    with _lock:
        s = _ensure(tier)
        s["total"] += 1
        s["consecutive_failures"] = 0


def record_failure(tier: str, error_msg: str) -> None:
    with _lock:
        s = _ensure(tier)
        s["total"] += 1
        s["failures"] += 1
        s["consecutive_failures"] += 1
        s["last_error"] = error_msg
        s["last_error_time"] = time.time()


def should_warn_user(tier: str) -> bool:
    """Return True once when consecutive failures reach the threshold.

    Resets the counter so the warning doesn't repeat every turn.
    """
    with _lock:
        s = _stats.get(tier)
        if not s or s["consecutive_failures"] < WARN_THRESHOLD:
            return False
        s["consecutive_failures"] = 0
        return True


def get_warning_message(tier: str) -> str:
    return (
        f"\n\n⚠️ 技能路由模型({tier})最近连续失败，"
        "部分功能可能无法正常触发。建议在管理面板检查模型配置。"
    )


def get_health() -> dict:
    """Return health summary for all tiers."""
    with _lock:
        result = {}
        for tier, s in _stats.items():
            total = s["total"]
            failures = s["failures"]
            result[tier] = {
                "total": total,
                "failures": failures,
                "success_rate": round((total - failures) / total, 3) if total else 1.0,
                "consecutive_failures": s["consecutive_failures"],
                "last_error": s["last_error"],
                "last_error_time": s["last_error_time"],
            }
        return result


def reset() -> None:
    """Clear all stats (for testing)."""
    with _lock:
        _stats.clear()
