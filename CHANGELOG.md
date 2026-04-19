# Changelog

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
