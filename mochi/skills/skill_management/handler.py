"""Skill Management — list / toggle / configure skills at runtime."""

import logging
import os

from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

_AGENT_SETTING_FIELDS = {
    "sleep_after_hour": (
        "SLEEP_AFTER_HOUR", "int", 1, 24,
        "每天从哪个本地小时起进入休息时段；24 表示午夜。",
    ),
    "wake_earliest_hour": (
        "WAKE_EARLIEST_HOUR", "int", 0, 23,
        "用户消息最早能唤醒你的本地小时。",
    ),
    "timezone_offset_hours": (
        "TIMEZONE_OFFSET_HOURS", "float", -12, 14,
        "本地时间相对 UTC 的小时偏移。",
    ),
    "max_daily_proactive": (
        "MAX_DAILY_PROACTIVE", "int", 0, 50,
        "每天最多送达多少条 Free Time/Attention 主动消息。",
    ),
}


class SkillManagementSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        tool = context.tool_name
        args = context.args

        if tool == "list_skills":
            return self._list_skills()
        elif tool == "toggle_skill":
            return self._toggle_skill(args.get("skill_name", ""), args.get("enabled", True))
        elif tool == "get_skill_config":
            return self._get_skill_config(args.get("skill_name", ""))
        elif tool == "set_skill_config":
            return self._set_skill_config(
                args.get("skill_name", ""),
                args.get("key", ""),
                args.get("value", ""),
            )
        elif tool == "manage_agent_settings":
            if not context.owner_authorized:
                return SkillResult(
                    output="只有 Owner 可以查看或调整运行设置。",
                    success=False,
                    error_code="owner_authorization_required",
                    retryable=False,
                )
            return self._manage_agent_settings(args)

        return SkillResult(output=f"Unknown tool: {tool}", success=False)

    def _manage_agent_settings(self, args: dict) -> SkillResult:
        action = args.get("action", "")
        if action == "view":
            return self._get_agent_settings()
        if action == "set":
            return self._set_agent_setting(
                str(args.get("key") or ""),
                args.get("value"),
            )
        return SkillResult(
            output="action 必须是 view 或 set。",
            success=False,
            error_code="invalid_action",
            retryable=True,
        )

    def _get_agent_settings(self) -> SkillResult:
        from mochi.admin.admin_db import get_system_config

        lines = ["当前运行设置："]
        for key, (system_key, _type, minimum, maximum, description) in (
            _AGENT_SETTING_FIELDS.items()
        ):
            lines.append(
                f"- {key} = {get_system_config(system_key)} "
                f"(范围 {minimum}–{maximum})\n  {description}"
            )
        return SkillResult(output="\n".join(lines))

    def _set_agent_setting(self, key: str, value) -> SkillResult:
        from mochi.admin.admin_db import (
            get_system_config,
            set_system_override,
        )

        field = _AGENT_SETTING_FIELDS.get(key)
        if field is None:
            return SkillResult(
                output=(
                    f"未知运行设置 '{key}'。可调整项："
                    + ", ".join(_AGENT_SETTING_FIELDS)
                ),
                success=False,
                error_code="unknown_setting",
                retryable=True,
            )
        system_key, type_name, minimum, maximum, description = field
        if isinstance(value, bool):
            return SkillResult(
                output=f"{key} 必须是数字。",
                success=False,
                error_code="invalid_setting_value",
                retryable=True,
            )
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return SkillResult(
                output=f"{key} 必须是数字。",
                success=False,
                error_code="invalid_setting_value",
                retryable=True,
            )
        if type_name == "int":
            if not numeric.is_integer():
                return SkillResult(
                    output=f"{key} 必须是整数。",
                    success=False,
                    error_code="invalid_setting_value",
                    retryable=True,
                )
            normalized: int | float = int(numeric)
        else:
            normalized = numeric
        if not minimum <= normalized <= maximum:
            return SkillResult(
                output=f"{key} 必须在 {minimum}–{maximum} 之间。",
                success=False,
                error_code="setting_out_of_range",
                retryable=True,
            )

        wake_hour = (
            normalized
            if key == "wake_earliest_hour"
            else int(get_system_config("WAKE_EARLIEST_HOUR"))
        )
        sleep_hour = (
            normalized
            if key == "sleep_after_hour"
            else int(get_system_config("SLEEP_AFTER_HOUR"))
        )
        if wake_hour >= sleep_hour:
            return SkillResult(
                output="最早清醒时间必须早于休息时段起点。",
                success=False,
                error_code="invalid_awake_window",
                retryable=True,
            )

        old_value = get_system_config(system_key)
        if old_value == normalized:
            return SkillResult(
                output=f"{key} 已经是 {normalized}，无需修改。",
                summary=f"Runtime setting {key} remains {normalized}.",
            )
        set_system_override(system_key, str(normalized))
        new_value = get_system_config(system_key)
        return SkillResult(
            output=f"已调整运行设置：{key}: {old_value} → {new_value}\n{description}",
            summary=f"Runtime setting {key} changed from {old_value} to {new_value}.",
            state_changed=True,
        )

    # ── list_skills ──────────────────────────────────────────

    def _list_skills(self) -> SkillResult:
        from mochi.skills import get_skill_info_all

        infos = get_skill_info_all()
        # Sort: tool-type first, then alphabetically
        infos.sort(key=lambda s: (0 if s["type"] == "tool" else 1, s["name"]))

        lines = []
        for s in infos:
            if s["auto_disabled"]:
                missing = ", ".join(s["config_missing"])
                status = f"AUTO_OFF (缺: {missing})"
            elif s["admin_disabled"]:
                status = "OFF"
            else:
                status = "ON"

            tools_str = ", ".join(s["tools"]) if s["tools"] else "(none)"
            config_tag = " [has config]" if s["config_schema"] else ""
            lines.append(
                f"• {s['name']} [{status}] — {s['description']}\n"
                f"  type={s['type']}, tools: {tools_str}{config_tag}"
            )

        return SkillResult(
            output=f"Registered skills ({len(infos)}):\n\n" + "\n\n".join(lines),
        )

    # ── toggle_skill ─────────────────────────────────────────

    def _toggle_skill(self, skill_name: str, enabled: bool) -> SkillResult:
        from mochi.skills import get_skill, refresh_capability_summary
        from mochi.db import get_disabled_skills, set_skill_enabled

        skill = get_skill(skill_name)
        if not skill:
            return SkillResult(output=f"Unknown skill: '{skill_name}'", success=False)

        # Core skills cannot be disabled
        if not enabled and skill.locked:
            return SkillResult(
                output=f"核心技能 '{skill_name}' 无法关闭。",
                success=False,
            )

        # Auto-disabled skills cannot be manually enabled
        if enabled and getattr(skill, "_config_missing", []):
            missing = ", ".join(skill._config_missing)
            return SkillResult(
                output=f"无法启用 '{skill_name}' — 缺少必要配置: {missing}。请先配置后重启。",
                success=False,
            )

        was_enabled = skill_name not in get_disabled_skills()
        set_skill_enabled(skill_name, enabled)
        refresh_capability_summary()
        action = "已启用" if enabled else "已禁用"
        return SkillResult(
            output=f"技能 '{skill_name}' {action}，立即生效。",
            state_changed=was_enabled != enabled,
        )

    # ── get_skill_config ─────────────────────────────────────

    def _get_skill_config(self, skill_name: str) -> SkillResult:
        from mochi.skills import get_skill
        from mochi.db import get_skill_config
        from mochi.skill_config_resolver import _env_key

        skill = get_skill(skill_name)
        if not skill:
            return SkillResult(output=f"Unknown skill: '{skill_name}'", success=False)

        schema = skill._config_schema_typed
        if not schema:
            return SkillResult(output=f"技能 '{skill_name}' 没有可配置项。")

        db_overrides = get_skill_config(skill_name)
        # Keys that should be masked (internal or typically secret)
        secret_keys = {f.key for f in schema if f.internal}
        secret_keys |= set(getattr(skill, "requires_config", []))

        lines = [f"Config for '{skill_name}':\n"]
        for field in schema:
            if field.internal:
                continue

            env_name = _env_key(skill_name, field.key)
            db_val = db_overrides.get(field.key)
            env_val = os.getenv(env_name)

            if db_val is not None:
                source = "db"
            elif env_val is not None:
                source = "env"
            else:
                source = "default"

            current = skill.config.get(field.key, field.default)
            display = "***" if (field.key in secret_keys and current) else current
            lines.append(
                f"• {field.key} = {display} (source: {source}, type: {field.type})\n"
                f"  {field.description}\n"
                f"  default: {field.default}"
            )

        return SkillResult(output="\n\n".join(lines))

    # ── set_skill_config ─────────────────────────────────────

    def _set_skill_config(self, skill_name: str, key: str, value: str) -> SkillResult:
        from mochi.skills import get_skill, refresh_capability_summary
        from mochi.db import (
            delete_skill_config,
            get_skill_config,
            set_skill_config,
        )
        from mochi.skill_config_resolver import _cast

        skill = get_skill(skill_name)
        if not skill:
            return SkillResult(output=f"Unknown skill: '{skill_name}'", success=False)

        schema_map = {f.key: f for f in skill._config_schema_typed}
        if key not in schema_map:
            valid_keys = ", ".join(schema_map.keys()) if schema_map else "(none)"
            return SkillResult(
                output=f"技能 '{skill_name}' 没有配置项 '{key}'。可用: {valid_keys}",
                success=False,
            )

        # Empty value = clear DB override
        if not value:
            changed = key in get_skill_config(skill_name)
            delete_skill_config(skill_name, key)
            skill.refresh_config()
            new_val = skill.config.get(key)
            refresh_capability_summary()
            return SkillResult(
                output=f"已清除 '{skill_name}.{key}' 的自定义值，当前使用: {new_val}",
                state_changed=changed,
            )

        # Validate type
        field = schema_map[key]
        try:
            _cast(value, field.type)
        except (ValueError, TypeError):
            return SkillResult(
                output=f"值 '{value}' 不符合类型 '{field.type}'。",
                success=False,
            )

        changed = get_skill_config(skill_name).get(key) != value
        set_skill_config(skill_name, key, value)
        skill.refresh_config()
        new_val = skill.config.get(key)
        refresh_capability_summary()
        return SkillResult(
            output=f"已设置 '{skill_name}.{key}' = {new_val}（已保存到数据库，立即生效）",
            state_changed=changed,
        )
