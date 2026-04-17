"""Skill Management — list / toggle / configure skills at runtime."""

import logging
import os

from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)


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

        return SkillResult(output=f"Unknown tool: {tool}", success=False)

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
        from mochi.db import set_skill_enabled

        skill = get_skill(skill_name)
        if not skill:
            return SkillResult(output=f"Unknown skill: '{skill_name}'", success=False)

        # Core skills cannot be disabled
        if not enabled and getattr(skill, "core", False):
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

        set_skill_enabled(skill_name, enabled)
        refresh_capability_summary()
        action = "已启用" if enabled else "已禁用"
        return SkillResult(output=f"技能 '{skill_name}' {action}，立即生效。")

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
        from mochi.db import set_skill_config, delete_skill_config
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
            delete_skill_config(skill_name, key)
            skill.refresh_config()
            new_val = skill.config.get(key)
            refresh_capability_summary()
            return SkillResult(
                output=f"已清除 '{skill_name}.{key}' 的自定义值，当前使用: {new_val}",
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

        set_skill_config(skill_name, key, value)
        skill.refresh_config()
        new_val = skill.config.get(key)
        refresh_capability_summary()
        return SkillResult(
            output=f"已设置 '{skill_name}.{key}' = {new_val}（已保存到数据库，立即生效）",
        )
