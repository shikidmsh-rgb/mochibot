# Changelog

## v0.8.13
- 优化 agent.md / 记忆与便签工具描述，提升 LLM 工具调用准确率（E2E 10/10）

## v0.8.12
- 新增 tool group 机制（core/extended），memory 工具改为每轮注入，扩展工具按需 request_tools
- 重写 agent.md / memory / note skill prompt，明确四种存储边界

## v0.8.11
- 修复跨长时间间隔后 LLM 时间感知错乱（晚安后第二天回复仍续说晚安）

## v0.8.10
- Anthropic Claude 4.x 强制 thinking 移除，修复 heartbeat / router / 提醒文案 400 报错
- 时区配置 admin portal 改完即时生效（最长 60 秒），无需重启
- 升级页面版本号显示修复 + 更新完成后自动重启

## v0.8.9
- Todo skill 路由改进

## v0.8.8
- 工具升级机制改进
- Escalation 预算调优

## v0.8.7
- 逻辑日期一致性修复
- Admin 重启稳定性

## v0.8.6
- Heartbeat 坚持感增强
- 用量追踪（reasoning + cached tokens）
- 多模型兼容层
- Admin 重启 + 提醒清理

## v0.8.5
- Router 可靠性修复（JSON mode）
- LLM 框架层 json_mode 支持

## v0.8.4
- Workspace skill（日记 + 文件编辑）
- 模型健康监控
- 气泡上限提升

## v0.8.3
- Reminder skill 升级
- Admin 一键更新
- Google Gemini 支持
- Heartbeat Think V2
- Note 批量编辑
- 时区 / Gemini / Embedding 修复

## v0.8.2
- ChatGPT 聊天记录搬家
- Skill 开关管理
- Heartbeat 改进
