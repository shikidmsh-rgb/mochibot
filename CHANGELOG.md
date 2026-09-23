# Changelog

## Unreleased

## v1.0.17

bugfix

## v1.0.16

- **个人工作区**：统一保存 Markdown 资料和开发个人小工具；Mochi 可自行编写、调试、启用工具，无需修改官方源码或重启。
- **个人扩展兼容接口**：提供 Mod v1，保留旧格式支持，个人代码、配置和数据不随官方更新被覆盖。
- **改善对话与行动连续性**：已送达的主动消息和近期工具操作进入上下文，静默操作、失败和未完成状态也能被 Main 看见。
- **恢复联网搜索**：百度千帆优先、Bing 免密钥备用；切换来源和时间筛选未生效时会明确说明。

升级提示：个人扩展开发默认开启，可在后台关闭；关闭开发不影响资料和已安装工具。扩展运行的是可信本地 Python，并非沙箱。默认工具循环上限由 5 轮提高到 16 轮，普通工具调用总上限由 8 次提高到 24 次，复杂任务可能增加耗时与费用；显式配置的限制仍然生效。

## v1.0.15
- 主动消息不再补发旧稿，定时提醒超过 5 分钟自动过期。
- 微信主动消息合并发送，减少打扰和额度消耗。
- 改善微信重启后的发送恢复与错误诊断。

## v1.0.14
- **新增 Mochi Files**：自主保存、查阅和编辑私人 Markdown 资料。
- **支持聊天调整设置**：作息、时区和主动消息上限。
- **减少重复晚安**：已经告别时，可以安静入睡。
- **修复记忆误去重**：保留否定、修正和补充，不因相似而误丢或覆盖。

## v1.0.13
- 工具调用更可靠，参数错误和真实改动状态更清楚
- 修复 DeepSeek 思考模式在工具续轮中的 400 中断
- Admin 可查看记忆来源；Web 搜索结果会明确标记为外部数据

## v1.0.1
- 修复模型表单误关闭、模型测试假成功、旧主动消息重启后补发和 Observer 缓存丢失
- Admin 支持复用已有模型凭据，并允许配置 HTTPS OpenAI-compatible Chat Completions 端点
- Main 自主判断睡眠切换；Free Time 带入最近完整对话，静默结果不再误吞正常回复
- 重写 Main 运行契约，明确陪伴关系、环境、工具与事实边界
- Memory Item 聚焦可长期复用的用户记忆，以用户消息证据提供真实日期并移除 category 运行语义
- 关系图改由 Weekly Main 谨慎维护，仅保留有 Memory Item 用户证据的人、宠物、地点与生活关系
- 精简设置首页，并移除旧 Notes 迁移、预留数据表、旧配置兼容、Legacy Skill Parser 和宽泛 Provider fallback

## v1.0.0
- 主人格统一接管聊天、睡前整理、每周维护、自主空闲关注和自我提醒
- Main + Lite 双模型运行时；支持 OpenAI、DeepSeek、Anthropic 和 Gemini
- 自由文本 Core 成为长期人格与关系上下文的唯一来源，旧 Notes 自动迁移
- 连续对话摘要、批量记忆提取、无 Embedding 召回和来源可追溯的知识图谱
- 按轮次提供工具，并统一为 `resident`、`routed`、`on_demand` 三种加载方式
- Observer 只读观察缓存与 `look_around` 感知能力
- 精简 Admin 与首次 Agent 设置流程
- 移除 Oura、独立 Note Skill、Deep tier、旧 Heartbeat Think 和通用风险等级
- Telegram 单图理解（OpenAI、DeepSeek、Anthropic、Gemini）
- 修复 Workspace 文件路径可越过 `data/` 边界的问题
- Gemini 和 DeepSeek 通过官方 OpenAI 兼容端点接入，不再安装原生 Gemini SDK
- 校正文档中的路由默认值、Provider 数量、通道能力和数据隐私说明

## v0.8.10
- 时区 bug 优化
- 记忆系统优化，不再经常忘记记录

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
