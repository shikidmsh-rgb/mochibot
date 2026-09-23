"""Workspace skill — diary read/write."""

from datetime import date, timedelta

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
        content = args.get("content")
        expected = args.get("_expected_content")
        day = args.get("day", "today")
        source_date = args.get("_source_date")
        target_date = args.get("_target_date")
        if not isinstance(content, str):
            return SkillResult(output="Error: content is required.", success=False)
        if day not in {"today", "tomorrow"}:
            return SkillResult(output="Error: day must be today or tomorrow.", success=False)
        if not isinstance(source_date, str) or not isinstance(target_date, str):
            return SkillResult(
                output="Diary target context is unavailable. Try again next turn.",
                success=False,
            )
        try:
            source = date.fromisoformat(source_date)
            target = date.fromisoformat(target_date)
        except ValueError:
            return SkillResult(
                output="Diary target context is invalid. Try again next turn.", success=False,
            )
        if target != (source if day == "today" else source + timedelta(days=1)):
            return SkillResult(
                output="Diary target changed during this turn. Try again.", success=False,
            )
        try:
            if not isinstance(expected, str):
                if day == "today":
                    return SkillResult(
                        output="Diary update context is unavailable. Try again next turn.",
                        success=False,
                    )
                current = diary.read_tomorrow_draft(target_date)
                return SkillResult(
                    output=(
                        "Tomorrow's journal needs a current snapshot; no write was applied."
                        f"\n\nCurrent journal:\n{current}"
                    ),
                    success=False,
                    document_snapshot=current,
                )
            if day == "today":
                result = diary.replace_section_exact(
                    "今日日記", expected_content=expected,
                    content=content, target_date=target_date,
                )
            else:
                result = diary.replace_tomorrow_exact(
                    source_date=source_date, target_date=target_date,
                    expected_content=expected, content=content,
                )
        except ValueError as exc:
            try:
                current = (
                    diary.read(section="今日日記") if day == "today"
                    else diary.read_tomorrow_draft(target_date)
                )
            except ValueError as read_error:
                return SkillResult(
                    output=f"Tomorrow Diary draft is unavailable: {read_error}",
                    success=False,
                )
            return SkillResult(
                output=f"Diary update rejected: {exc}\n\nCurrent journal:\n{current}",
                success=False,
                document_snapshot=current,
            )
        label = "Today's" if day == "today" else "Tomorrow's"
        receipt = (
            f"{label} journal ({target_date}) "
            f"{'updated' if result['changed'] else 'unchanged'} ({result['chars']} chars)."
        )
        return SkillResult(
            output=receipt, summary=receipt,
            entity_refs=[f"diary:{target_date}"],
            state_changed=result["changed"],
            document_snapshot=result["content"],
        )

    def _read_diary(self, args: dict) -> SkillResult:
        date_str = (args.get("date") or "").strip()
        if not date_str:
            content = diary.read_raw()
            return SkillResult(
                output=content if content else "Today's diary is empty.",
                document_snapshot=diary._section_content(content, "今日日記") if content else "",
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
