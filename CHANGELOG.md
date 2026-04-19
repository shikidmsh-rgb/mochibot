# Changelog

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
