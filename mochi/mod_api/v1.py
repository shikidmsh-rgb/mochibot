"""Stable personal extension API, shared with the existing Skill runtime.

Keep these public types and helper semantics compatible when internal runtime
implementations change. Legacy imports from mochi.skills.base remain supported.
"""

from pathlib import Path

from mochi.skills.base import Skill, SkillContext, SkillResult

__all__ = ["Skill", "SkillContext", "SkillResult", "run_candidate"]


def run_candidate(
    extension_id: str,
    package_dir: Path,
    data_dir: Path,
    *,
    config: dict | None = None,
) -> Skill:
    """Load a candidate with explicit fixture config, without live registration."""
    from mochi.extensions.worker import run_candidate as load_candidate

    return load_candidate(extension_id, package_dir, data_dir, config=config)
