# Changelog

## v0.8.8

### 修复
- **`request_tools` escalation 闭环修复**：当 LLM 调 `request_tools` 申请技能但填错名字时，旧实现只回 `"No valid skills found."`，LLM 拿不到任何线索就放弃 → 装作"系统封锁了/我没工具"。现在失败时返回完整的 `available_skills` 清单（name → description）+ hint，LLM 可以立即重试选对名字。同时：
  - `skills` 参数从 comma-string 改为 array（更结构化）
  - 工具名（如 `edit_habit`）自动映射到父技能（`habit`），避免 LLM 填错
  - 失败的 escalation 不再消耗 `TOOL_ESCALATION_MAX_PER_TURN` 预算（之前会，导致填错两次就废）
  - 已禁用的技能不会被批准
  - description 改为强制语气："ALWAYS call this immediately when you need a skill that wasn't provided"

### 改进
- `TOOL_ESCALATION_MAX_PER_TURN` 默认值 2 → 4（对齐上游 mochi）。失败已不消耗预算，4 次成功上限给单轮多步任务足够空间。

## v0.8.7

### 修复
- **logical_today / wall-clock 混用 bug**：凌晨 0-3 点（maintenance window）期间，多处代码"写入用 wall-clock、查询用 logical_today"导致数据错位 —— 例如凌晨 1 点说"今天跑了步"打卡进了 X 表但 habit 列表查 Y 表看起来像没打卡，凌晨 2 点的宵夜 meal 在日记里看不见，habit streak / pause 日期算错。本次系统性梳理 17 个文件，统一规则：用户认知的"今天"业务数据（habit / meal / note / proactive 限流 / morning briefing / bedtime tidy）走 `logical_today()`；物理事实（消息计数、Oura API、LLM 系统时间、note archive 文件名月份）保持 wall-clock。
- Admin Windows 重启更稳定（`c089d4e`）：uvicorn 的 `capture_signals` 不再 re-raise SIGBREAK，避免 bot 重启时把 admin 进程一起带走。

### 改进
- 新增 `logical_days_ago(n)` helper（`mochi/config.py`），统一所有 logical 日期回推路径，避免散布的 wall-clock 写法重新引入混用 bug。
- 新增 `tests/test_logical_today_lint.py` —— grep-lint meta 测试，扫描 `mochi/` 下所有 wall-clock `YYYY-MM-DD` 构造，要求每处都标注 `# wall-clock 故意：<原因>` 注释或转换成 logical helper。这是防止后续回归的持久防御。
- 新增 6 个 maintenance window 一致性回归测试（`tests/test_logical_today_consistency.py`）+ 2 个 helper 单元测试。

### 一次性升级副作用
- 升级当天，0-3 点窗口内**修复前**的 proactive_log 旧记录（按 wall-clock 写）在新规则下会被 `get_today_proactive_sent` 视为"昨天的"，所以"今日已发主动消息"的统计在升级首日可能略低。这是预期的一次性行为，旧数据不迁移。

## v0.8.6

### 新功能
- Heartbeat 支持跨重试表达坚持感
- 用量追踪：reasoning tokens 和 cached prompt tokens 可在 /cost 查看

### 改进
- LLM 兼容性增强：Claude 4.x prompt caching + extended thinking，Gemini / OpenAI 兼容层适配 reasoning 模型
- Heartbeat 架构重构：Think（扫描）与 Chat（语音）职责分离
- E2E 测试覆盖扩展

### 修复
- Admin 重启更健壮，支持 Windows 孤儿进程清理
- 提醒删除改为硬删除，日记仅显示未触发提醒
- reasoning 模型输出的 JSON 提取更鲁棒

## v0.8.5

### Bug Fixes
- 修复了 bot 频繁"听不懂话"的问题 —— router 的 JSON 解析失败率从最高 75% 降到 0%（通过用 LLM 原生 JSON Mode 取代 prompt 求模型输出 JSON）
- 修复 diary 在 skill registry 为空时被 `refresh_diary_status` 误清空数据的 bug
- `dump_think_prompt` 工具现在与实际 `_think()` 的 prompt 组装逻辑保持一致
- 强化 chat / think prompt 中的当前时间提示
- proactive / reminder prompt 补齐 `|||` 气泡分隔符

### Improvements
- LLM 框架层引入 `json_mode` 参数：OpenAI/Azure 走 `response_format` + 能力缓存 +  BadRequest 1-shot 重试，Gemini 走 `response_mime_type`，Anthropic 在框架层统一剥 markdown 围栏
- 将原本散落在 3 处的 markdown-strip 兜底逻辑（heartbeat、migration、memory_engine）整合到框架层统一处理
- 新增 23 个 LLM JSON mode 单元测试覆盖围栏剥离 / OpenAI 能力缓存 / Gemini 配置 / Anthropic 触发条件

## v0.8.4

### New Features
- Workspace skill —— 日记读写 + 文件编辑，core skill 不可禁用
- Model Health 监控 —— 模型健康状态追踪 + Admin Portal 可视化面板
- Bubble 上限提升 —— 最大气泡数从 4 提升到 8

### Improvements
- Agent prompt 重写 —— 高层能力概览，更清晰的自我认知

### Bug Fixes
- 修复微信渠道主动消息不发的 bug（`OWNER_USER_ID` 检查改用 `is None`，user_id=0 被误判为 falsy）

## v0.8.3

### New Features
- Reminder skill 升级 —— 事件驱动计时器 + 诊断日志增强
- Admin Portal 一键更新 —— 检查版本、查看更新日志、一键拉取+重启
- Google Gemini provider 支持（chat + embedding）
- Heartbeat Think V2 —— 活人感输出增强
- Note 批量编辑（rewrite action）

### Bug Fixes
- 统一时区处理（全模块使用 config.TZ）
- Gemini API 兼容性修复（model name、连续同角色消息合并等）
- Embedding 维度自动检测 + 向量表自动重建
- Admin worktree 支持、untracked files 误报修复
- Windows 优雅关闭、Admin UI 隐私修复

## v0.8.2

### New Features
- ChatGPT 聊天记录搬家
- Skill 管理（/skilloff /skillon，聊天内开关 skill 省 token）
- Heartbeat 优化

### Bug Fixes
- 迁移记忆去重
- 历史消息去掉时间戳前缀
