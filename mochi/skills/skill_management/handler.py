"""Main's single settings entry, backed by the shared settings service."""

import json
import logging
import sqlite3
from copy import deepcopy

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi import settings

log = logging.getLogger(__name__)


class SkillManagementSkill(Skill):
    def get_tools(self) -> list[dict]:
        definitions = deepcopy(super().get_tools())
        for definition in definitions:
            definition["function"]["parameters"]["additionalProperties"] = False
        return definitions

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = args.get("action")
        setting_id = args.get("id", "")
        writing = action in {"set", "reset"}
        try:
            if context.actor != "main" or context.trigger != "tool_call":
                raise settings.SettingsError(
                    "main_required", "此入口仅供 Mochi 的工具调用使用。",
                )
            settings.validate_arguments(args)
            if writing and not setting_id.startswith("tools.") and not (
                context.owner_authorized and context.source == "chat"
            ):
                raise settings.SettingsError(
                    "user_authorization_required",
                    "此设置需要用户在对话中授权修改。",
                )
            if action == "list":
                data = settings.list_settings(args.get("group"))
                summary = "已查看设置目录。"
            elif action == "get":
                data = settings.get_setting(setting_id)
                summary = f"已查看设置 {setting_id}。"
            else:
                data = settings.change_setting(
                    setting_id, args.get("value"),
                    reset=action == "reset", user_id=context.user_id,
                )
                summary = (
                    f"设置 {setting_id} 未变化。" if not data["changed"]
                    else f"已重置设置 {setting_id}。" if action == "reset"
                    else f"已修改设置 {setting_id}。"
                )
            return SkillResult(
                output=json.dumps(data, ensure_ascii=False, allow_nan=False),
                summary=summary,
                entity_refs=[setting_id] if setting_id else [],
                state_changed=bool(data.get("changed", False)),
            )
        except settings.SettingsError as exc:
            return SkillResult(
                output=str(exc), success=False, error_code=exc.code,
                retryable=exc.code in {"invalid_arguments", "unknown_setting", "invalid_setting_value"},
                state_changed=exc.changed, state_change_unknown=exc.unknown,
            )
        except (sqlite3.Error, OSError, ValueError):
            log.exception("Settings operation failed: %s %s", action, setting_id)
            return SkillResult(
                output=(
                    "设置未能保存；无法确认是否已有部分更改。" if writing
                    else f"设置读取失败：{setting_id}。"
                ),
                success=False,
                error_code="settings_save_failed" if writing else "settings_read_failed",
                retryable=False, state_change_unknown=writing,
            )
