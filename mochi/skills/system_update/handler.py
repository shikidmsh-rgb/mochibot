"""Owner-requested official updates; never exit before final delivery."""

from copy import deepcopy
from functools import partial

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.update_service import (
    UpdateError, check_for_update, prepare_update, request_update_exit, stage_update,
)


class SystemUpdateSkill(Skill):
    def get_tools(self) -> list[dict]:
        definitions = deepcopy(super().get_tools())
        for definition in definitions:
            definition["function"]["parameters"]["additionalProperties"] = False
        return definitions

    async def execute(self, context: SkillContext) -> SkillResult:
        if context.tool_name not in {"check_system_update", "install_system_update"}:
            return SkillResult(
                output=f"Unknown tool: {context.tool_name}",
                success=False, error_code="unknown_tool", retryable=False,
            )
        if context.actor != "main" or context.trigger != "tool_call" or context.source != "chat":
            return SkillResult(
                output="系统更新只接受用户当前对话中的明确请求。",
                success=False, error_code="update_requires_owner_chat", retryable=False,
            )
        if not context.owner_authorized:
            return SkillResult(
                output="只有用户可以更新 MochiBot。",
                success=False, error_code="owner_authorization_required", retryable=False,
            )
        if context.args:
            return SkillResult(
                output="系统更新工具不接受参数。",
                success=False, error_code="invalid_arguments", retryable=True,
            )
        try:
            release = await check_for_update()
            summary = (
                f"Checked official MochiBot releases; current v{release.current_version}, "
                f"latest stable v{release.version}."
            )
            if not release.available:
                return SkillResult(
                    output=(
                        f"当前版本 v{release.current_version}，最新官方正式版 "
                        f"v{release.version}；没有更高版本可安装。"
                    ),
                    summary=summary, content_source="external_web",
                )
            if context.tool_name == "check_system_update":
                notes = f"\n\n更新说明：\n{release.notes}" if release.notes else ""
                if release.notes_truncated:
                    notes += "\n（更新说明已截断）"
                return SkillResult(
                    output=(
                        f"发现官方正式版 v{release.version}，"
                        f"当前是 v{release.current_version}。{notes}"
                    ),
                    summary=summary, content_source="external_web",
                )
            prepared = await prepare_update(release)
            request = stage_update(
                prepared, user_id=context.user_id, channel_id=context.channel_id,
                transport=context.transport, turn_id=context.turn_id,
            )
        except UpdateError as exc:
            return SkillResult(
                output=str(exc), success=False, error_code=exc.code,
                retryable=exc.retryable, state_changed=exc.code_updated,
            )
        return SkillResult(
            output=(
                f"已准备从 v{release.current_version} 更新到官方正式版 v{release.version}。"
                "当前最终回复确认送达后才会开始更新和重启；现在尚未安装。"
            ),
            summary=(
                f"Prepared MochiBot update to v{release.version}; "
                "installation awaits final-reply delivery."
            ),
            state_changed=request["changed"],
            after_delivery=(
                partial(request_update_exit, request["request_id"])
                if request["changed"] else None
            ),
        )
