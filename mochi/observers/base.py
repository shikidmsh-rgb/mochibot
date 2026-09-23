"""Observer base class and OBSERVATION.md parser.

Observers are the Heartbeat's eyes — they collect structured data from the
world (weather, habits, sensors, etc.) on a timer, with zero LLM calls.

Every observer directory must have:
  - OBSERVATION.md  (metadata + field docs)
  - observer.py     (collection logic)
  - __init__.py

Lifecycle:
  - safe_observe() checks interval, calls observe(), caches result
  - On error: logs warning, returns stale cache, never crashes heartbeat
  - After 5 consecutive failures: stops trying (disabled in collect_all)
"""

import json
import math
import os
import re
import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypedDict

from mochi.config import TZ

log = logging.getLogger(__name__)

VIEW_TEXT_LIMIT = 160
OVERVIEW_ITEM_LIMIT = 3
DETAIL_ITEM_LIMIT = 10

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_TOKEN_LIKE_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")


class CachedObserverView(TypedDict):
    """Safe, read-only projection of one observer's cached state."""

    source: str
    collected_at: str | None
    available: bool
    stale: bool
    error: str | None
    failure_count: int
    facts: dict[str, Any]
    mode: Literal["overview", "detail"]


def bounded_view_text(value: Any) -> str:
    """Return compact text safe for cache views and model context."""
    text = _CONTROL_CHARS_RE.sub(" ", str(value))
    text = _URL_RE.sub("[url]", text)
    text = _TOKEN_LIKE_RE.sub("[redacted]", text)
    text = " ".join(text.split())
    if len(text) > VIEW_TEXT_LIMIT:
        return text[: VIEW_TEXT_LIMIT - 1] + "…"
    return text


def safe_error_text(error: Any) -> str:
    """Sanitize an observer error without exposing request or credential data."""
    return bounded_view_text(error)


@dataclass
class ObserverMeta:
    """Parsed from OBSERVATION.md front matter."""
    name: str = ""
    interval: int = 20          # minutes between collections
    enabled: bool = True
    requires_config: list[str] = field(default_factory=list)
    skill_name: str = ""        # owning skill (empty = standalone observer)


def _parse_observation_md(md_path: str) -> ObserverMeta:
    """Parse OBSERVATION.md front matter into ObserverMeta.

    Expected format:
      ---
      name: weather
      interval: 30
      enabled: true
      requires_config: [WEATHER_CITY]
      ---
    """
    meta = ObserverMeta()

    if not os.path.exists(md_path):
        return meta

    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not fm_match:
        return meta

    for line in fm_match.group(1).strip().split("\n"):
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()

        if key == "name":
            meta.name = val
        elif key == "interval":
            try:
                meta.interval = int(val)
            except ValueError:
                pass
        elif key == "enabled":
            meta.enabled = val.lower() in ("true", "yes", "1")
        elif key == "requires_config":
            # Parse [KEY1, KEY2] or KEY1, KEY2
            keys = re.findall(r"[A-Z_][A-Z0-9_]+", val)
            meta.requires_config = keys
        elif key == "skill_name":
            meta.skill_name = val

    return meta


class Observer(ABC):
    """Base class for all observers.

    Subclass and implement observe() — return a flat dict of data.
    Everything else (caching, interval, error handling) is handled here.
    """

    def __init__(self) -> None:
        self._meta: ObserverMeta | None = None
        self._last_collected_at: datetime | None = None
        self._last_data: dict = {}
        self._consecutive_errors: int = 0
        self._last_error: str | None = None

    @property
    def meta(self) -> ObserverMeta:
        """Parsed OBSERVATION.md metadata (lazy-loaded and cached)."""
        if self._meta is None:
            # OBSERVATION.md lives next to the observer subclass file
            import inspect
            class_file = os.path.abspath(inspect.getfile(self.__class__))
            class_dir = os.path.dirname(class_file)
            md_path = os.path.join(class_dir, "OBSERVATION.md")
            self._meta = _parse_observation_md(md_path)
            if not self._meta.name:
                self._meta.name = self._observer_dir()
        return self._meta

    def _observer_dir(self) -> str:
        """Directory name of this observer (used as fallback name)."""
        # e.g. /observers/weather/observer.py -> "weather"
        return os.path.basename(
            os.path.dirname(os.path.abspath(
                __import__("inspect").getfile(self.__class__)
            ))
        )

    @property
    def name(self) -> str:
        return self.meta.name or self.__class__.__name__.lower()

    @property
    def effective_interval(self) -> int:
        """Interval in minutes, with DB override support.

        Priority: DB override (_observer:{name}) > OBSERVATION.md default.
        """
        try:
            from mochi.db import get_skill_config
            overrides = get_skill_config(f"_observer:{self.name}")
            if "interval" in overrides:
                return max(1, int(overrides["interval"]))
        except Exception:
            pass
        return self.meta.interval

    @abstractmethod
    async def observe(self) -> dict:
        """Collect data. Return a flat dict of observations.

        Return {} if nothing to report.
        Raise on error — safe_observe() handles it.
        """
        ...

    def should_collect(self, now: datetime) -> bool:
        """Whether enough time has passed since last collection."""
        if self._last_collected_at is None:
            return True
        elapsed = (now - self._last_collected_at).total_seconds() / 60
        return elapsed >= self.effective_interval

    async def safe_observe(self) -> dict:
        """Wrapper: checks interval, calls observe(), caches result, handles errors."""
        now = datetime.now(TZ)

        if not self.should_collect(now):
            return self._last_data  # return cached, not time yet

        try:
            data = await self.observe()
            self._last_data = data
            self._last_collected_at = now
            self._consecutive_errors = 0
            self._last_error = None
            return data
        except Exception as e:
            self._consecutive_errors += 1
            self._last_error = safe_error_text(e)
            log.warning(
                "Observer %s failed (%d consecutive): %s",
                self.name, self._consecutive_errors, e,
            )
            if self._consecutive_errors >= 5:
                log.error(
                    "Observer %s hit 5 consecutive failures — "
                    "will be skipped until restart",
                    self.name,
                )
            return self._last_data  # stale cache, never crash heartbeat

    @staticmethod
    def _bounded_scalar(value: Any) -> str | int | float | bool | None:
        """Copy one JSON scalar, bounding and sanitizing text values."""
        if isinstance(value, str):
            return bounded_view_text(value)
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return None

    def select_view(
        self,
        data: dict,
        *,
        scalar_fields: tuple[str, ...] = (),
        list_fields: dict[str, tuple[str, ...]] | None = None,
        item_limit: int = OVERVIEW_ITEM_LIMIT,
    ) -> dict[str, Any]:
        """Allowlist and bound scalar fields and shallow structured lists."""
        if not isinstance(data, dict):
            return {}

        facts: dict[str, Any] = {}
        for field_name in scalar_fields:
            if field_name not in data:
                continue
            value = self._bounded_scalar(data[field_name])
            if value is not None:
                facts[field_name] = value

        for field_name, item_fields in (list_fields or {}).items():
            raw_items = data.get(field_name)
            if not isinstance(raw_items, list):
                continue

            items: list[Any] = []
            for raw_item in raw_items[:item_limit]:
                if isinstance(raw_item, dict) and item_fields:
                    item = self.select_view(
                        raw_item,
                        scalar_fields=item_fields,
                        item_limit=item_limit,
                    )
                    if item:
                        items.append(item)
                elif not item_fields:
                    item = self._bounded_scalar(raw_item)
                    if item is not None:
                        items.append(item)
            facts[field_name] = items

        return facts

    def overview_view(self, data: dict) -> dict[str, Any]:
        """Project cached data for compact consumption. Default-deny."""
        return {}

    def detail_view(self, data: dict) -> dict[str, Any]:
        """Project cached data for detailed consumption. Default-deny."""
        return {}

    def cached_view(
        self,
        *,
        enabled: bool,
        detail: bool = False,
        now: datetime | None = None,
    ) -> CachedObserverView:
        """Build a safe view from cache without collecting or mutating state."""
        mode: Literal["overview", "detail"] = "detail" if detail else "overview"
        collected_at = (
            self._last_collected_at.isoformat()
            if self._last_collected_at is not None
            else None
        )
        base: CachedObserverView = {
            "source": self.name,
            "collected_at": collected_at,
            "available": False,
            "stale": True,
            "error": None,
            "failure_count": self._consecutive_errors,
            "facts": {},
            "mode": mode,
        }

        if not enabled:
            base["error"] = "source_disabled"
            return base
        if self._last_collected_at is None or not self._last_data:
            base["error"] = "not_collected"
            return base

        current = now or datetime.now(TZ)
        collected = self._last_collected_at
        if collected.tzinfo is None and current.tzinfo is not None:
            collected = collected.replace(tzinfo=current.tzinfo)
        stale_after = max(
            self.effective_interval * 2,
            self.effective_interval + 5,
        )
        age_minutes = (current - collected).total_seconds() / 60
        base["available"] = True
        base["stale"] = age_minutes > stale_after
        base["error"] = (
            safe_error_text(self._last_error)
            if self._consecutive_errors and self._last_error
            else None
        )

        try:
            source_data = deepcopy(self._last_data)
            projector = self.detail_view if detail else self.overview_view
            facts = projector(source_data)
            if not isinstance(facts, dict):
                raise TypeError("observer view projection must return a dict")
            json.dumps(facts, ensure_ascii=False, allow_nan=False)
            base["facts"] = deepcopy(facts)
        except Exception:
            log.warning(
                "Observer %s safe view projection failed",
                self.name,
                exc_info=True,
            )
            base["available"] = False
            base["stale"] = True
            base["error"] = "projection_error"
            base["facts"] = {}

        return base

    def has_delta(self, prev: dict, curr: dict) -> bool:
        """Check if observation changed meaningfully since last collection.

        Default: any change triggers delta. Override in subclasses to
        suppress noisy sources (e.g., weather changes are not actionable).
        """
        return prev != curr
