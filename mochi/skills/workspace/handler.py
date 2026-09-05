"""Workspace skill — diary read/write."""

from mochi.diary import diary
from mochi.skills.base import Skill, SkillContext, SkillResult


class WorkspaceSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        tool_name, args = context.tool_name, context.args
        if tool_name == "write_diary":
            return self._write_diary(args)
        elif tool_name == "read_diary":
            return self._read_diary(args)
        return SkillResult(output=f"Unknown tool: {tool_name}", success=False)

    def _write_diary(self, args: dict) -> SkillResult:
        entry = (args.get("entry") or "").strip()
        if not entry:
            return SkillResult(output="Error: entry is required.", success=False)
        before = diary.read_raw()
        output = diary.append(entry, source="chat", section="今日日記")
        return SkillResult(
            output=output,
            state_changed=diary.read_raw() != before,
        )

    def _read_diary(self, args: dict) -> SkillResult:
        date_str = (args.get("date") or "").strip()
        if not date_str:
            content = diary.read_raw()
            return SkillResult(
                output=content if content else "Today's diary is empty."
            )

        try:
            year_month = date_str[:7]
            archive_dir = diary.path.parent / "diary_archive"
            archive_path = archive_dir / f"{year_month}.md"
            if not archive_path.exists():
                return SkillResult(
                    output=f"No diary archive found for {year_month}.",
                    success=False,
                )

            raw = archive_path.read_text(encoding="utf-8")
            lines = raw.split("\n")
            collecting = False
            result: list[str] = []
            for line in lines:
                if line.startswith("# Diary ") and date_str in line:
                    collecting = True
                    result.append(line)
                elif collecting and line.startswith("# Diary "):
                    break
                elif collecting:
                    result.append(line)

            if not result:
                return SkillResult(
                    output=f"No diary entry found for {date_str}.",
                    success=False,
                )
            return SkillResult(output="\n".join(result).strip())
        except Exception as e:
            return SkillResult(
                output=f"Error reading diary archive: {e}",
                success=False,
            )
