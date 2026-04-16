"""{{Skill Name}} Observer — {{one-line description}}.

Optional file. Only add an observer when heartbeat needs to be aware of
this skill's state (e.g. upcoming reminders, active counts, sensor data).
Most skills (translation, search, etc.) do NOT need an observer.

See also: OBSERVATION.md in this directory for metadata.
"""

import logging

from mochi.observers.base import Observer

log = logging.getLogger(__name__)


class {{SkillName}}Observer(Observer):
    """{{What this observer surfaces for heartbeat.}}"""

    # Optional: override has_delta to control when this observer triggers Think.
    # Default (inherited): any change in observe() output triggers Think.
    # Return False to never trigger Think on your own (piggyback on other observers).
    #
    # def has_delta(self, prev: dict, curr: dict) -> bool:
    #     return prev.get("some_key") != curr.get("some_key")

    async def observe(self) -> dict:
        from mochi.config import OWNER_USER_ID
        # Import your skill's queries here (deferred to avoid circular imports)
        # from mochi.skills.{{name}}.queries import some_query

        user_id = OWNER_USER_ID
        if not user_id:
            return {}

        # Query your skill's data and return a flat dict.
        # Return {} if no data available — the observer key will be absent
        # from the heartbeat observation dict entirely.
        return {}
