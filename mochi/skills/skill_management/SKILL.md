---
name: skill_management
description: "技能管理 — 列出所有技能及状态、开关技能、查看与修改技能配置"
type: tool
expose_as_tool: true
tier: chat
core: true
---

# Skill Management

## Usage Rules

- 用户问"你有什么技能/功能"时，调用 `list_skills` 列出全部
- 开关技能前先确认用户意图，核心技能无法关闭
- 修改配置后告知用户新值和生效状态
- 不要主动建议用户关闭或修改技能，除非用户明确要求

## Tools

### list_skills (L0)
列出所有已注册技能及其状态。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|

### toggle_skill (L1)
启用或禁用一个技能。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |
| enabled | boolean | yes | true=启用, false=禁用 |

### get_skill_config (L0)
查看某个技能的配置项及当前值。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |

### set_skill_config (L1)
修改某个技能的配置值（写入数据库，立即生效）。传空 value 可清除自定义值、恢复默认。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |
| key | string | yes | 配置项名称 |
| value | string | yes | 新值（空字符串=清除自定义值） |
