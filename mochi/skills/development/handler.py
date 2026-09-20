"""Main-authored personal development, using explicit file and execution tools."""

import json
from pathlib import Path

from mochi.skills.base import Skill, SkillContext, SkillResult

_GUIDE = Path(__file__).resolve().parents[3] / "docs" / "extensions.md"


def _text(args: dict, key: str, *, empty: bool = False) -> str:
    value = args.get(key)
    if not isinstance(value, str) or (not empty and not value):
        raise ValueError(f"{key} must be {'a string' if empty else 'a nonempty string'}")
    return value


class DevelopmentSkill(Skill):
    async def execute(self, context: SkillContext) -> SkillResult:
        from mochi.db import get_disabled_skills
        from mochi.extensions import runner, store, template

        if context.actor != "main":
            return SkillResult(
                output="Development tools are available only to Main.",
                success=False, error_code="main_required", retryable=False,
            )
        if "development" in get_disabled_skills():
            return SkillResult(
                output="Development is currently disabled. Its existing skill toggle controls availability.",
                success=False, error_code="development_disabled", retryable=False,
            )

        args = context.args
        tool = context.tool_name
        changed = False
        try:
            if tool == "inspect_extension":
                action = _text(args, "action")
                if action == "guide":
                    name = args.get("extension_id", "local_example")
                    store.extension_root(name)
                    payload = {
                        "guide": _GUIDE.read_text(encoding="utf-8"),
                        "extension_id": name,
                        "template": template.files(name),
                        "run_script": "smoke.py",
                    }
                elif action == "list":
                    from mochi.skills import get_skill_info_all
                    payload = {
                        "extensions": [
                            info for info in get_skill_info_all()
                            if info["source"] == "personal"
                            and (
                                not args.get("extension_id")
                                or info["name"] == args["extension_id"]
                            )
                        ],
                    }
                elif action == "read":
                    payload = store.read_files(
                        _text(args, "extension_id"), args.get("paths", []),
                        area=args.get("area", "draft"),
                        offset=args.get("offset", 0), limit=args.get("limit", 12000),
                    )
                elif action == "report":
                    payload = runner.read_last_run(_text(args, "extension_id"))
                else:
                    raise ValueError(f"Unknown inspect action: {action}")
            elif tool == "write_extension":
                name = _text(args, "extension_id")
                action = _text(args, "action")
                if action == "create":
                    supplied = {}
                    for key in ("skill_md", "handler_py", "smoke_py"):
                        if key in args:
                            supplied[key] = _text(args, key, empty=True)
                    payload = store.scaffold(name, **supplied)
                elif action == "write":
                    payload = store.write_file(
                        name, _text(args, "path"), _text(args, "content", empty=True),
                    )
                elif action == "edit":
                    payload = store.edit_file(
                        name, _text(args, "path"), _text(args, "old_text"),
                        _text(args, "new_text", empty=True),
                    )
                elif action == "remove":
                    payload = store.remove_file(name, _text(args, "path"))
                else:
                    raise ValueError(f"Unknown write action: {action}")
                changed = True
            elif tool == "run_extension":
                payload = await runner.run_extension(
                    _text(args, "extension_id"),
                    script=args.get("script", "smoke.py"),
                    arguments=args.get("arguments"),
                )
                success = bool(payload["success"] and payload.get("report_saved"))
                return SkillResult(
                    output=json.dumps(payload, ensure_ascii=False),
                    success=success,
                    error_code="" if success else (
                        "extension_timeout" if payload.get("timed_out")
                        else "run_report_failed" if not payload.get("report_saved")
                        else "extension_run_failed"
                    ),
                    retryable=False if not success else None,
                    summary=(
                        f"Personal extension {args['extension_id']} script "
                        f"{'completed' if payload['success'] else 'failed'}; "
                        f"run report {'saved' if payload.get('report_saved') else 'not saved'}; "
                        "this does not activate the extension."
                    ),
                    entity_refs=[f"extension:{args['extension_id']}"],
                    state_changed=bool(payload.get("report_saved")),
                    state_change_unknown=bool(payload.get("state_change_unknown")),
                )
            elif tool == "activate_extension":
                from mochi.skills import activate_extension
                name = _text(args, "extension_id")
                payload = activate_extension(name)
                changed = True
            else:
                raise ValueError(f"Unknown development tool: {tool}")
        except (OSError, ValueError) as exc:
            return SkillResult(
                output=str(exc), success=False,
                error_code=getattr(exc, "code", "invalid_extension_operation"),
                retryable=True,
                state_changed=bool(getattr(exc, "state_changed", False)),
                state_change_unknown=bool(getattr(exc, "state_change_unknown", False)),
                summary=f"Personal development operation {tool} failed.",
            )

        return SkillResult(
            output=json.dumps(payload, ensure_ascii=False),
            state_changed=changed,
            summary=f"Personal development {tool}: {args.get('action', 'activate')} completed.",
            entity_refs=(
                [f"extension:{args['extension_id']}"] if args.get("extension_id") else []
            ),
        )
