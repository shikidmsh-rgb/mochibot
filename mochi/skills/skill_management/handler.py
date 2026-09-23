"""Skill Management — list / toggle / configure skills at runtime."""

import logging
import os
import sqlite3
from copy import deepcopy

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
        "MAX_DAILY_PROACTIVE", "int", 0, 10,
        "每天最多获得多少次 Free Time 自主思考机会；实际次数可能更少。",
    ),
}


class SkillManagementSkill(Skill):

    def get_tools(self) -> list[dict]:
        definitions = deepcopy(super().get_tools())
        for definition in definitions:
            function = definition["function"]
            if function["name"] == "manage_tool_load":
                function["parameters"].update(
                    additionalProperties=False,
                    anyOf=[
                        {"properties": {"action": {"enum": ["pin"]}}, "required": ["load"]},
                        {
                            "properties": {
                                "action": {"enum": ["reset"]},
                                "tool_name": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    ],
                )
        return definitions

    async def execute(self, context: SkillContext) -> SkillResult:
        tool = context.tool_name
        args = context.args

        if tool == "list_skills":
            return self._list_skills()
        elif tool == "manage_tool_load":
            return self._manage_tool_load(context)
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
            if s.get("load_error"):
                status = "LOAD_ERROR"
            elif s["admin_disabled"]:
                status = "OFF"
            elif not s.get("loaded", True):
                status = "NOT_LOADED"
            elif s["auto_disabled"]:
                missing = ", ".join(s["config_missing"])
                status = f"AUTO_OFF (缺: {missing})"
            else:
                status = "ON"

            tool_lines = []
            for tool in s["tool_loads"]:
                load = (
                    tool["effective"] if tool["declared"] == tool["effective"]
                    else f"{tool['declared']} → {tool['effective']}"
                )
                pin = f", pinned={tool['pinned']}" if tool["pinned"] else ""
                changed = f", changed={tool['changed_at']}" if tool["changed_at"] else ""
                reason_label = "Nightly snapshot" if tool["adaptive"] and not tool["pinned"] else "reason"
                tool_lines.append(
                    f"{tool['name']} [{load}{pin}{changed}; {reason_label}: {tool['reason']}]"
                )
            tools_str = ", ".join(tool_lines) if tool_lines else "(none)"
            config_tag = " [has config]" if s["config_schema"] else ""
            lines.append(
                f"• {s['name']} [{status}] — {s['description']}\n"
                f"  type={s['type']}, tools: {tools_str}{config_tag}"
                + ("\n  草稿可由 Mochi 使用 activate_extension 启用，无需重启。" if s.get("activation_required") else "")
                + (f"\n  {s['load_error']}" if s.get("load_error") else "")
                + (
                    f"\n  development={'ON' if s['development_enabled'] else 'OFF'}; "
                    "documents remain available."
                    if "development_enabled" in s else ""
                )
            )

        return SkillResult(
            output=f"Skills ({len(infos)}):\n\n" + "\n\n".join(lines),
        )

    def _manage_tool_load(self, context: SkillContext) -> SkillResult:
        if context.actor != "main" or context.trigger != "tool_call":
            return SkillResult(
                output="只有 Main 可以调整工具加载层级。",
                success=False, error_code="main_required", retryable=False,
            )
        args = context.args
        action = args.get("action")
        tool_name = args.get("tool_name")
        if action not in {"pin", "reset"} or not isinstance(tool_name, str) or not tool_name.strip():
            return SkillResult(
                output="action 必须是 pin 或 reset，并提供 tool_name。",
                success=False, error_code="invalid_arguments", retryable=True,
            )
        tool_name = tool_name.strip()
        if action == "pin" and args.get("load") not in {"on_demand", "routed"}:
            return SkillResult(
                output="pin 需要 load=on_demand 或 routed。",
                success=False, error_code="invalid_arguments", retryable=True,
            )
        if action == "reset" and "load" in args:
            return SkillResult(
                output="reset 不接受 load。",
                success=False, error_code="invalid_arguments", retryable=True,
            )
        from mochi.skills import get_declared_tools
        from mochi.adaptive_tool_load import AdaptiveLoadError, pin_definition

        definition = next((
            tool for tool in get_declared_tools()
            if tool.get("function", {}).get("name") == tool_name
        ), None)
        if definition is None:
            return SkillResult(
                output=f"Unknown tool: '{tool_name}'",
                success=False, error_code="unknown_tool", retryable=False,
            )
        try:
            state = pin_definition(
                definition, args.get("load") if action == "pin" else None,
                user_id=context.user_id,
            )
        except AdaptiveLoadError as exc:
            message = (
                f"工具 '{tool_name}' 的加载层级由技能合同固定，不能调整。"
                if exc.code == "fixed_tool_load"
                else f"工具 '{tool_name}' 的自适应加载声明无效，未修改。"
            )
            return SkillResult(
                output=message, success=False, error_code=exc.code, retryable=False,
            )
        except sqlite3.Error:
            log.exception("Could not persist adaptive tool load")
            return SkillResult(
                output="工具加载设置未能保存，请稍后重试。",
                success=False, error_code="tool_load_save_failed", retryable=True,
                state_change_unknown=True,
            )
        action_text = (
            f"已锁定为 {state['effective_load']}" if action == "pin"
            else "已恢复自动调整"
        )
        changed = bool(state["changed"])
        return SkillResult(
            output=f"{tool_name} {action_text}。{state['reason']}",
            summary=(
                f"Updated adaptive load for {tool_name}." if changed
                else f"Adaptive load for {tool_name} is unchanged."
            ),
            entity_refs=[f"tool:{tool_name}"],
            state_changed=changed,
        )

    # ── toggle_skill ─────────────────────────────────────────

    def _toggle_skill(self, skill_name: str, enabled: bool) -> SkillResult:
        from mochi.extensions.store import ExtensionError
        from mochi.skills import get_skill_for_management, load_installed_extension, refresh_capability_summary
        from mochi.db import get_disabled_skills, set_skill_enabled

        if type(enabled) is not bool:
            return SkillResult(
                output="enabled must be a boolean.", success=False,
                error_code="invalid_arguments", retryable=False,
            )
        if skill_name == "development":
            from mochi.personal_workspace import set_development_enabled

            changed = set_development_enabled(enabled)
            return SkillResult(
                output=f"Personal workspace development {'enabled' if enabled else 'disabled'}; "
                "documents and installed personal tools are unchanged.",
                state_changed=changed,
            )
        skill = get_skill_for_management(skill_name)
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
                output=f"无法启用 '{skill_name}' — 缺少必要配置: {missing}。请先补齐配置。",
                success=False,
            )

        was_enabled = skill_name not in get_disabled_skills()
        set_skill_enabled(skill_name, enabled)
        load_error = ""
        unknown_effects = False
        loaded = True
        if enabled and skill.external:
            try:
                loaded = load_installed_extension(skill_name)
            except ExtensionError as exc:
                loaded = False
                load_error = str(exc)
                unknown_effects = exc.state_change_unknown
        refresh_capability_summary()
        action = "已启用" if enabled else "已禁用"
        effect = (
            f"加载失败：{load_error}" if load_error
            else "尚无已安装工具；Mochi 可使用 activate_extension 启用草稿，无需重启。"
            if not loaded
            else "立即生效。"
        )
        return SkillResult(
            output=f"技能 '{skill_name}' {action}，{effect}",
            success=not bool(load_error),
            error_code="extension_load_failed" if load_error else "",
            state_change_unknown=unknown_effects,
            state_changed=was_enabled != enabled,
        )

    # ── get_skill_config ─────────────────────────────────────

    def _get_skill_config(self, skill_name: str) -> SkillResult:
        from mochi.skills import get_skill_configuration
        from mochi.db import get_skill_config
        from mochi.skill_config_resolver import _env_key

        skill = get_skill_configuration(skill_name)
        if not skill:
            return SkillResult(output=f"Unknown skill: '{skill_name}'", success=False)

        schema = skill._config_schema_typed
        if not schema:
            return SkillResult(output=f"技能 '{skill_name}' 没有可配置项。")

        db_overrides = get_skill_config(skill_name)
        # Keys that should be masked (internal or typically secret)
        secret_keys = {f.key for f in schema if f.internal or f.secret}
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
                f"  default: {'***' if field.key in secret_keys and field.default else field.default}"
            )

        return SkillResult(output="\n\n".join(lines))

    # ── set_skill_config ─────────────────────────────────────

    def _set_skill_config(self, skill_name: str, key: str, value: str) -> SkillResult:
        from mochi.skills import get_skill_configuration, refresh_skill_configuration
        from mochi.db import (
            delete_skill_config,
            get_skill_config,
            set_skill_config,
        )
        from mochi.skill_config_resolver import _cast

        skill = get_skill_configuration(skill_name)
        if not skill:
            return SkillResult(output=f"Unknown skill: '{skill_name}'", success=False)

        schema_map = {f.key: f for f in skill._config_schema_typed}
        if key not in schema_map:
            valid_keys = ", ".join(schema_map.keys()) if schema_map else "(none)"
            return SkillResult(
                output=f"技能 '{skill_name}' 没有配置项 '{key}'。可用: {valid_keys}",
                success=False,
            )
        field = schema_map[key]
        secret = field.secret or field.internal or key in skill.requires_config

        # Empty value = clear DB override
        if not value:
            changed = key in get_skill_config(skill_name)
            delete_skill_config(skill_name, key)
            skill.refresh_config()
            new_val = "***" if secret else skill.config.get(key)
            refresh_skill_configuration(skill_name)
            return SkillResult(
                output=f"已清除 '{skill_name}.{key}' 的自定义值，当前使用: {new_val}",
                state_changed=changed,
            )

        # Validate type
        try:
            _cast(value, field.type)
        except (ValueError, TypeError):
            return SkillResult(
                output=f"配置值不符合类型 '{field.type}'。",
                success=False,
            )

        changed = get_skill_config(skill_name).get(key) != value
        set_skill_config(skill_name, key, value)
        skill.refresh_config()
        new_val = "***" if secret else skill.config.get(key)
        refresh_skill_configuration(skill_name)
        return SkillResult(
            output=f"已设置 '{skill_name}.{key}' = {new_val}（已保存到数据库，立即生效）",
            state_changed=changed,
        )
