"""Resolve skill values and their provenance from the same priority chain."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mochi.skills.base import ConfigField

ConfigValue = str | int | float | bool


@dataclass(frozen=True)
class ResolvedConfig:
    value: ConfigValue
    source: str


def _cast(value: str, type_name: str) -> ConfigValue:
    if type_name == "bool":
        normalized = str(value).strip().lower()
        if normalized not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("Invalid boolean configuration")
        return normalized in {"true", "1", "yes"}
    if type_name == "int":
        return int(value)
    if type_name == "float":
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite configuration")
        return number
    return str(value)


def _env_key(skill_name: str, config_key: str) -> str:
    return f"SKILL_{skill_name.upper()}_{config_key.upper()}"


def resolve_skill_config_details(
    skill_name: str, schema: list[ConfigField],
) -> dict[str, ResolvedConfig]:
    from mochi.admin.admin_crypto import decrypt_api_key
    from mochi.db import get_skill_config

    overrides = get_skill_config(skill_name)
    result = {}
    for field in schema:
        if field.key in overrides:
            raw = overrides[field.key]
            source = "database"
            if field.secret:
                decoded = decrypt_api_key(raw)
                if raw and not decoded:
                    raise ValueError("已保存的凭据无法读取，需要用户检查加密配置。")
                raw = decoded
        elif _env_key(skill_name, field.key) in os.environ:
            raw = os.environ[_env_key(skill_name, field.key)]
            source = "environment"
        elif field.key in os.environ:
            raw = os.environ[field.key]
            source = "environment"
        else:
            raw = field.default
            source = "default"
        try:
            value = _cast(raw, field.type)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置值无效：{skill_name}.{field.key}。") from exc
        result[field.key] = ResolvedConfig(value, source)
    return result


def resolve_skill_config(skill_name: str, schema: list[ConfigField]) -> dict[str, ConfigValue]:
    return {
        key: resolved.value
        for key, resolved in resolve_skill_config_details(skill_name, schema).items()
    }
