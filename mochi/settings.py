"""One settings catalog and mutation path shared by Main and Admin.

Values remain in their existing stores. Catalog reads do not initialize model
clients, import inactive extension handlers, or contact external services.
"""

from __future__ import annotations

from dataclasses import dataclass

from mochi.skill_config_resolver import _cast, resolve_skill_config_details

GROUPS = {
    "runtime": "运行设置",
    "skills": "技能与个人开发",
    "tools": "工具加载",
    "models": "模型配置摘要",
}
_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}
_ARGUMENT_ERROR = (
    "参数不符合此操作：list 只接受可选 group；get、reset 只接受 id；"
    "set 需要 id 和字符串 value。"
)


class SettingsError(ValueError):
    def __init__(self, code: str, message: str, *, changed: bool = False, unknown: bool = False):
        super().__init__(message)
        self.code = code
        self.changed = changed
        self.unknown = unknown


@dataclass(frozen=True)
class RuntimeField:
    name: str
    description: str
    minimum: int | float
    maximum: int | float


RUNTIME_FIELDS = {
    "sleep_after_hour": RuntimeField(
        "休息时段起点",
        "每天从哪个本地小时起进入计划休息时段；24 表示午夜。进入时段不等于立刻睡着。",
        1, 24,
    ),
    "wake_earliest_hour": RuntimeField(
        "消息唤醒起点", "每天从哪个本地小时起，用户的消息可以唤醒正在休息的你。", 0, 23,
    ),
    "fallback_wake_hour": RuntimeField(
        "自动起床时间", "无人发消息时，在本地这个小时自动唤醒；不在计划休息时段内唤醒。", 0, 23,
    ),
    "timezone_offset_hours": RuntimeField(
        "本地时区", "本地时间相对 UTC 的小时偏移；影响日历、作息和按本地时间计算的任务。",
        -12, 14,
    ),
    "max_daily_proactive": RuntimeField(
        "每日 Free Time 机会数",
        "每天最多获得多少次 Free Time 自主思考机会；实际次数可能更少，不是主动消息条数。设为 0 时不安排 Free Time。",
        0, 10,
    ),
}


def validate_arguments(args: dict) -> None:
    action = args.get("action")
    allowed = {
        "list": {"action", "group"}, "get": {"action", "id"},
        "set": {"action", "id", "value"}, "reset": {"action", "id"},
    }
    if not isinstance(action, str) or action not in allowed or set(args) - allowed[action]:
        raise SettingsError("invalid_arguments", _ARGUMENT_ERROR)
    if action == "list":
        if "group" in args and args["group"] not in GROUPS:
            raise SettingsError("invalid_arguments", _ARGUMENT_ERROR)
    elif not isinstance(args.get("id"), str) or not args["id"]:
        raise SettingsError("invalid_arguments", _ARGUMENT_ERROR)
    if action == "set" and not isinstance(args.get("value"), str):
        raise SettingsError("invalid_arguments", _ARGUMENT_ERROR)


def _entry(
    setting_id: str, name: str, description: str, value: object, type_name: str,
    source: str, *, reset: str = "", effect: str = "", writable: bool = True,
    authority: str = "user_request", **details,
) -> dict:
    return {
        "id": setting_id, "group": setting_id.split(".", 1)[0],
        "name": name, "description": description, "value": value,
        "type": type_name, "source": source, "writable": writable,
        "resettable": writable and bool(reset), "reset": reset, "effect": effect,
        "authority": authority if writable else "read_only", **details,
    }


def _runtime_settings(setting_id: str = "") -> list[dict]:
    from mochi.admin.admin_db import SYSTEM_DEFAULTS, get_system_config, get_system_overrides

    overrides = get_system_overrides()
    result = []
    for key, field in RUNTIME_FIELDS.items():
        full_id = f"runtime.{key}"
        if setting_id and full_id != setting_id:
            continue
        system_key = key.upper()
        type_name, default = SYSTEM_DEFAULTS[system_key]
        result.append(_entry(
            full_id, field.name, field.description, get_system_config(system_key),
            _TYPES[type_name], "database" if system_key in overrides else "default",
            default=default, constraints={"minimum": field.minimum, "maximum": field.maximum},
            reset=f"恢复为 {default}，重启后保持。",
            effect=(
                "后续本地时间计算使用新时区；已安排任务的时间不会自动重写。"
                if key == "timezone_offset_hours" else
                "下次判断作息或安排 Free Time 时使用新值，不改变已开始的任务。"
            ),
        ))
    return result


def _skill_settings(setting_id: str = "") -> list[dict]:
    from mochi.db import get_disabled_skills
    from mochi.skills import get_skill_configuration, get_skill_info_all

    result = []
    disabled = get_disabled_skills()
    wanted = setting_id.split(".", 2)[1] if setting_id else ""
    for info in get_skill_info_all():
        name = info["name"]
        if wanted and name != wanted:
            continue
        enabled_id = f"skills.{name}.enabled"
        if not setting_id or setting_id == enabled_id:
            status = (
                "加载失败" if info["load_error"] else
                "已关闭" if info["admin_disabled"] else
                "尚未加载" if not info["loaded"] else
                "缺少必要配置" if info["config_missing"] else "已启用"
            )
            result.append(_entry(
                enabled_id, f"{name} · 启用", "启用或关闭此技能；关闭不删除已有数据。",
                not info["admin_disabled"], "boolean",
                "database" if info["admin_disabled"] else "default",
                writable=not info["locked"], default=True, status=status,
                reset="启用此技能；个人技能草稿仍需激活。",
                effect="影响后续技能使用，不中断正在进行的调用。",
                details={
                    "description": info["description"],
                    "loaded": info["loaded"], "locked": info["locked"],
                    "required": info["config_required"], "missing": info["config_missing"],
                    "activation_required": info["activation_required"],
                    "load_error": info["load_error"], "tools": info["tool_loads"],
                },
            ))
        skill = get_skill_configuration(name)
        if skill is None:
            continue
        fields = [
            field for field in skill._config_schema_typed
            if not field.internal and (
                not setting_id or setting_id == f"skills.{name}.config.{field.key}"
            )
        ]
        if not fields:
            continue
        resolved = resolve_skill_config_details(name, fields)
        from mochi.skills import get_skill
        live = get_skill(name)
        live_keys = {f.key for f in live._config_schema_typed} if live else set()
        for field in fields:
            current = resolved[field.key]
            result.append(_entry(
                f"skills.{name}.config.{field.key}", f"{name} · {field.label or field.key}",
                field.description,
                {"configured": bool(current.value)} if field.secret else current.value,
                _TYPES[field.type], current.source,
                default={"configured": bool(field.default)} if field.secret else _cast(field.default, field.type),
                reset="移除自定义值，改用环境配置或技能默认值；结果显示实际采用的值和来源。",
                effect=(
                    "下次天气查询使用新城市，不再沿用旧城市的缓存；修改设置本身不查询天气。"
                    if name == "weather" and field.key == "WEATHER_CITY" else
                    "后续调用使用新值；仅草稿使用的字段要等草稿激活。"
                ),
                draft_only=skill.external and field.key not in live_keys,
            ))
    development_id = "skills.personal_workspace.development_enabled"
    if not setting_id or setting_id == development_id:
        result.append(_entry(
            development_id, "个人开发",
            "允许修改、运行和激活个人技能源码；关闭不影响文档、只读查看或已安装工具。",
            "development" not in disabled, "boolean",
            "database" if "development" in disabled else "default",
            default=True, reset="开启个人开发。", effect="后续开发操作使用新开关。",
        ))
    return result


def _tool_settings(setting_id: str = "") -> list[dict]:
    from mochi.adaptive_tool_load import resolve_definition
    from mochi.db import get_adaptive_tool_load_states
    from mochi.skills import get_declared_tools

    states = get_adaptive_tool_load_states()
    result = []
    for definition in get_declared_tools():
        name = definition["function"]["name"]
        full_id = f"tools.{name}.load"
        if setting_id and setting_id != full_id:
            continue
        tool = resolve_definition(definition, states=states)
        adaptive = tool["_adaptive_load"]
        if not adaptive and not setting_id:
            continue
        result.append(_entry(
            full_id, f"{name} · 加载方式",
            (
                "设置为 on_demand 或 routed 会锁定加载方式；reset 恢复按真实使用记录自动调整，不改变工具权限。"
                if adaptive else "工具加载方式由技能合同固定，不能修改。"
            ),
            tool["_load"], "string",
            "declared" if not adaptive else "pinned" if tool["_load_pinned"] else "automatic",
            writable=adaptive,
            authority="main", constraints={"choices": ["on_demand", "routed"]},
            reset="恢复自动调整，并按现有使用记录重新计算加载方式。",
            effect="影响后续工具加载；需要立即使用尚未出现的工具时，可调用 request_tools。",
            details={
                "declared": tool["_declared_load"], "pinned": tool["_load_pinned"],
                "reason": tool["_load_reason"], "changed_at": tool["_load_changed_at"],
            },
        ))
    return result


def _model_settings(setting_id: str = "") -> list[dict]:
    from mochi.admin.admin_db import get_model, list_tier_assignments
    from mochi.admin.admin_env import read_env_value
    from mochi.image_service import get_image_config
    from mochi.model_pool import loaded_config_summary

    names = {
        "main": "Main 模型", "lite": "Lite 模型",
        "embedding": "Embedding", "image_generation": "图片生成模型",
    }
    assignments = list_tier_assignments()
    running = loaded_config_summary()
    result = []
    for key, name in names.items():
        if setting_id and setting_id != f"models.{key}":
            continue
        if key in {"main", "lite"}:
            model = get_model(assignments[key], mask_key=True) if key in assignments else None
            saved = {
                "provider": model["provider"] if model else "",
                "model": model["model"] if model else "",
                "base_url": model["base_url"] if model else "",
                "api_key_set": bool(model and model["api_key_set"]),
                "configured": bool(model and model["model"] and model["api_key_set"]),
            }
        elif key == "embedding":
            saved = {
                "provider": read_env_value("EMBEDDING_PROVIDER") or "none",
                "model": read_env_value("EMBEDDING_MODEL") or "",
                "base_url": read_env_value("EMBEDDING_BASE_URL") or "",
                "api_key_set": bool(read_env_value("EMBEDDING_API_KEY")),
            }
            saved["configured"] = bool(
                saved["provider"] == "openai" and saved["model"] and saved["api_key_set"]
            )
        else:
            image = get_image_config()
            saved = {
                "provider": image["resolved_protocol"], "model": image["model"],
                "base_url": image["base_url"], "api_key_set": image["api_key_set"],
                "configured": image["configured"],
            }
        result.append(_entry(
            f"models.{key}", name,
            "只读；可请用户在管理后台的模型页面修改。已配置不代表已确认服务可用。",
            {"saved": saved, "running": running.get(key)},
            "object", "environment" if key == "embedding" else "database",
            writable=False,
            effect="saved 是已保存配置，running 是当前已加载配置；尚未加载时无法确认运行状态。",
        ))
    return result


_READERS = {
    "runtime": _runtime_settings, "skills": _skill_settings,
    "tools": _tool_settings, "models": _model_settings,
}


def list_settings(group: str | None = None) -> dict:
    if group is not None and group not in GROUPS:
        raise SettingsError("invalid_arguments", _ARGUMENT_ERROR)
    fields = ("id", "name", "description", "value", "writable", "status")
    return {
        "groups": {key: name for key, name in GROUPS.items() if group is None or key == group},
        "items": [
            {key: entry[key] for key in fields if key in entry}
            for key, reader in _READERS.items() if group is None or key == group
            for entry in reader()
        ],
    }


def get_setting(setting_id: str) -> dict:
    group = setting_id.split(".", 1)[0]
    if "." not in setting_id:
        raise SettingsError("unknown_setting", f"未知设置：{setting_id}。可使用 list 查看当前目录。")
    reader = _READERS.get(group)
    if reader:
        entries = reader(setting_id)
        if entries:
            return entries[0]
    raise SettingsError("unknown_setting", f"未知设置：{setting_id}。可使用 list 查看当前目录。")


def normalize_runtime_values(values: dict[str, str]) -> dict[str, int | float]:
    from mochi.admin.admin_db import SYSTEM_DEFAULTS, get_system_config

    normalized = {}
    for key, raw in values.items():
        field = RUNTIME_FIELDS.get(key)
        if field is None:
            raise SettingsError("unknown_setting", f"未知设置：runtime.{key}。可使用 list 查看当前目录。")
        try:
            value = _cast(raw, SYSTEM_DEFAULTS[key.upper()][0])
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError
            if not field.minimum <= value <= field.maximum:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise SettingsError("invalid_setting_value", f"配置值不符合类型或范围：runtime.{key}。") from exc
        normalized[key] = value
    if set(values) & {"sleep_after_hour", "wake_earliest_hour", "fallback_wake_hour"}:
        hours = {
            key: normalized.get(key, get_system_config(key.upper()))
            for key in ("sleep_after_hour", "wake_earliest_hour", "fallback_wake_hour")
        }
        if (
            hours["wake_earliest_hour"] >= hours["sleep_after_hour"]
            or hours["fallback_wake_hour"] >= hours["sleep_after_hour"]
        ):
            raise SettingsError(
                "invalid_awake_window", "消息唤醒起点和自动起床时间必须早于休息时段起点。",
            )
    return normalized


def _write_skill_switch(name: str, enabled: bool) -> bool:
    from mochi.db import get_disabled_skills, set_skill_enabled
    from mochi.extensions.store import ExtensionError
    from mochi.skills import get_missing_config, get_skill_for_management, load_installed_extension

    if name == "development":
        from mochi.personal_workspace import set_development_enabled
        return set_development_enabled(enabled)
    skill = get_skill_for_management(name)
    if skill is None:
        raise SettingsError("unknown_setting", f"未知设置：skills.{name}.enabled。可使用 list 查看当前目录。")
    if skill.locked and not enabled:
        raise SettingsError("locked_skill", "核心技能不能关闭。")
    missing = get_missing_config(skill)
    if enabled and missing:
        raise SettingsError("missing_config", f"缺少必要配置，无法启用：{', '.join(missing)}。")
    changed = enabled != (name not in get_disabled_skills())
    set_skill_enabled(name, enabled)
    if enabled and skill.external:
        try:
            load_installed_extension(name)
        except ExtensionError as exc:
            raise SettingsError(
                "extension_load_failed", f"启用开关已保存，但个人技能加载失败：{exc}。",
                changed=changed, unknown=exc.state_change_unknown,
            ) from exc
    return changed


def _write_skill_value(name: str, key: str, value: str | None, *, reset: bool) -> bool:
    from mochi.admin.admin_crypto import encrypt_api_key, is_encrypted
    from mochi.db import delete_skill_config, get_skill_config, set_skill_config
    from mochi.skills import get_skill_configuration, refresh_skill_configuration

    skill = get_skill_configuration(name)
    field = next((
        f for f in skill._config_schema_typed if f.key == key and not f.internal
    ), None) if skill else None
    if field is None:
        raise SettingsError("unknown_setting", f"未知设置：skills.{name}.config.{key}。可使用 list 查看当前目录。")
    overrides = get_skill_config(name)
    if reset:
        changed = key in overrides
        delete_skill_config(name, key)
    else:
        assert isinstance(value, str)
        try:
            _cast(value, field.type)
        except (TypeError, ValueError) as exc:
            raise SettingsError("invalid_setting_value", f"配置值不符合类型或范围：skills.{name}.config.{key}。") from exc
        stored = value
        if field.secret and value:
            stored = encrypt_api_key(value)
            if not is_encrypted(stored):
                raise SettingsError(
                    "secret_encryption_failed", "凭据无法安全保存，未写入；需要用户检查加密配置。",
                )
        # Ciphertext is randomized; compare the resolved plaintext when deciding whether to write.
        current = resolve_skill_config_details(name, [field])[key].value
        changed = (
            key not in overrides or current != _cast(value, field.type)
            or bool(field.secret and value and not is_encrypted(overrides[key]))
        )
        if changed:
            set_skill_config(name, key, stored)
    refresh_skill_configuration(name)
    if changed and name == "weather" and key == "WEATHER_CITY":
        from mochi.observers import get_observer
        observer = get_observer("weather")
        if observer is not None:
            observer.invalidate()
            observer.meta.enabled = bool(
                resolve_skill_config_details(name, [field])[key].value
            )
    return changed


def change_setting(
    setting_id: str, value: str | None = None, *, reset: bool = False, user_id: int = 0,
) -> dict:
    from mochi.admin.admin_db import SYSTEM_DEFAULTS, set_system_override

    before = get_setting(setting_id)
    if not before["writable"]:
        if setting_id.startswith("tools."):
            raise SettingsError("fixed_tool_load", "工具加载方式由技能合同固定，不能修改。")
        raise SettingsError(
            "locked_skill" if setting_id.endswith(".enabled") else "read_only_setting",
            "核心技能不能关闭。" if setting_id.endswith(".enabled") else f"该设置只读：{setting_id}。",
        )
    if reset and not before["resettable"]:
        raise SettingsError("reset_not_supported", f"该设置不支持重置：{setting_id}。")
    if not reset and not isinstance(value, str):
        raise SettingsError("invalid_arguments", _ARGUMENT_ERROR)
    group, _, address = setting_id.partition(".")
    if group == "runtime":
        raw = str(SYSTEM_DEFAULTS[address.upper()][1]) if reset else value
        normalized = normalize_runtime_values({address: raw})[address]
        changed = before["value"] != normalized or before["source"] != "database"
        if changed:
            set_system_override(address.upper(), str(normalized))
    elif group == "skills":
        name, _, key = address.partition(".")
        if key == "enabled" or setting_id == "skills.personal_workspace.development_enabled":
            try:
                enabled = True if reset else _cast(value, "bool")
            except (TypeError, ValueError) as exc:
                raise SettingsError("invalid_setting_value", f"配置值不符合类型或范围：{setting_id}。") from exc
            changed = _write_skill_switch(
                "development" if key == "development_enabled" else name, bool(enabled),
            )
        else:
            changed = _write_skill_value(name, key.removeprefix("config."), value, reset=reset)
    else:
        from mochi.adaptive_tool_load import pin_definition
        from mochi.skills import get_declared_tools

        tool_name = address.removesuffix(".load")
        definition = next(
            tool for tool in get_declared_tools() if tool["function"]["name"] == tool_name
        )
        if not reset and value not in {"on_demand", "routed"}:
            raise SettingsError("invalid_setting_value", f"配置值不符合类型或范围：{setting_id}。")
        changed = pin_definition(definition, None if reset else value, user_id=user_id)["changed"]
    after = get_setting(setting_id)
    return {
        "id": setting_id, "action": "reset" if reset else "set", "changed": changed,
        "before": before["value"], "after": after,
        "message": "设置未变化。" if not changed else "设置已重置。" if reset else "设置已保存。",
    }
