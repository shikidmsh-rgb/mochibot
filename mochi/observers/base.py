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

import os
import re
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from mochi.config import TIMEZONE_OFFSET_HOURS

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))


@dataclass
class ObserverMeta:
    """Parsed from OBSERVATION.md front matter."""
    name: str = ""
    interval: int = 20          # minutes between collections
    enabled: bool = True
    requires_config: list[str] = field(default_factory=list)


def _parse_observation_md(md_path: str) -> ObserverMeta:
    """Parse OBSERVATION.md front matter into ObserverMeta.

    Expected format:
      ---
      name: weather
      interval: 30
      enabled: true
      requires_config: [OPENWEATHER_API_KEY, WEATHER_LAT]
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

    @property
    def meta(self) -> ObserverMeta:
        """Parsed OBSERVATION.md metadata (lazy-loaded and cached)."""
        if self._meta is None:
            # OBSERVATION.md lives next to observer.py
            md_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                self._observer_dir(),
                "OBSERVATION.md",
            )
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
        return elapsed >= self.meta.interval

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
            return data
        except Exception as e:
            self._consecutive_errors += 1
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
