---
name: skill_management
description: "运行设置与技能管理 — 调整作息、时区、每日 Free Time 思考上限，或管理已安装技能"
type: tool
locked: true
---

# Skill Management

## Capability Context

- `manage_agent_settings` 修改 Agent 的真实运行设置；Core 中的偏好文字不会改变调度行为。
- `max_daily_proactive` 限制每天的 Free Time 自主思考机会，不是主动消息条数；设为 0 时不安排 Free Time，活跃聊天期间不打断。
- `set` 用来落实用户在当前对话中提出的调整；成功回执包含实际生效的新值。
- `list_skills` 和 `get_skill_config` 读取实时注册状态及个人包元数据，不为列举而导入未加载的 Python；损坏或禁用的包也能管理。
- `toggle_skill` 与 `set_skill_config` 改变后续 provider round 可用的能力；新工具名仍需通过 `request_tools` 请求，不能在同一 provider response 中越过工具快照。
- `personal_workspace` 统一读写个人资料与工具草稿，也可运行草稿并实时启用工具。开发默认开启，无需单独授权或前往 Admin；`toggle_skill(skill_name="development", enabled=false)` 只关闭草稿修改、运行和启用，不影响资料读写、代码查看或已安装工具。这是兼容设置开关，不是另一套开发入口。
- 启用已安装的个人包会直接尝试加载；仅有草稿时回报需要 `activate_extension`。加载失败会明确报错并说明启用开关已保存，不把开关状态当作实际可用状态。
- 启停或改配置属于用户授权边界：只有用户对具体技能和改动的明确意图才授权写操作。核心技能在执行层无法关闭。
- 写操作的工具回执包含实际新值与生效状态，失败不会伪装成成功。
- 标注 adaptive 的工具默认按需加载；Nightly 根据最近 30 天普通对话中的成功使用，在 on_demand 与 routed 之间调整，不改变工具权限。
- Main 可以锁定层级或恢复自动调整；reset 清除锁定后按当前使用重新计算。加载层级变化不授予当前 provider response 中尚未可用的工具。

## Tools

### list_skills (on_demand, adaptive)
列出已注册技能及尚未加载的个人包，包括开关、配置、加载错误和是否需要激活。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|

### toggle_skill (on_demand)
启用或禁用一个技能。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称；development 表示个人工作区的开发开关 |
| enabled | boolean | yes | true=启用, false=禁用 |

### get_skill_config (on_demand, adaptive)
查看某个技能的配置项及当前值。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |

### set_skill_config (on_demand)
修改某个技能的配置值（写入数据库，立即生效）。传空 value 可清除自定义值、恢复默认。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| skill_name | string | yes | 技能名称 |
| key | string | yes | 配置项名称 |
| value | string | yes | 新值（空字符串=清除自定义值） |

### manage_agent_settings (routed)
查看或调整 Agent 自身面向用户的运行设置；`set` 落实用户明确提出的改变。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: view, set) | yes | 查看当前设置或修改一项设置 |
| key | string (enum: sleep_after_hour, wake_earliest_hour, timezone_offset_hours, max_daily_proactive) | no | set 时必填 |
| value | number | no | set 时必填；作息小时使用本地 24 小时制，Free Time 上限为 0–10 的整数 |

### manage_tool_load (on_demand)
按需锁定或恢复一个允许自适应的工具加载层级。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: pin, reset) | yes | pin = 锁定层级；reset = 恢复自动调整 |
| tool_name | string | yes | 工具名称 |
| load | string (enum: on_demand, routed) | no | pin 时必填；reset 时不传 |
