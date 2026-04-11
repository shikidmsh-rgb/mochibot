"""Diary skill — automation only (no LLM-exposed tools).

All diary file I/O is handled by mochi.diary (L4 infrastructure).
This handler exists only to make diary a toggleable skill unit.
"""

from mochi.skills.base import Skill, SkillContext, SkillResult


class DiarySkill(Skill):
    async def execute(self, context: SkillContext) -> SkillResult:
        return SkillResult(output="Diary is automation-only.", success=True)
