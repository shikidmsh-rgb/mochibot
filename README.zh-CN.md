<div align="center">

[English](README.md) | [中文](README.zh-CN.md)

# 🍡 MochiBot

**一个开源的 AI 陪伴 bot——记得你、关心你、和你一起成长。**

*不只是聊天机器人，是一个有温度的伙伴。*

**给那些想要一个像朋友一样的 AI，而不只是搜索框的人。**<br>
情绪支持。每日问候。温柔提醒。永久记忆。完全私有。

</div>

---

## 为什么选 MochiBot

- **轻量级** — 单进程、SQLite，不需要 Docker/Redis/Postgres。`pip install` 就能跑
- **持久记忆** — 三层记忆系统，重启不丢失，每晚自动整理。支持全文搜索和可选的向量搜索
- **主动关心** — 心跳循环主动找你聊，而不是干等你输入
- **完全私有** — 自托管，数据永远不离开你的机器
- **易扩展** — 即插即用的 Skills 和 Observers，启动时自动发现。支持丰富的元数据、使用规则和灵活的触发配置
- **省钱** — 5 级模型路由：简单任务用便宜模型，复杂任务才上强模型
- **感知身体** — 内置 [Oura Ring](https://ouraring.com) 集成：睡眠、准备度、活动、压力——你说不出口的，身体数据替你开口

---

## 设计理念

### 三层记忆

```
Layer 1: 核心记忆    — 精炼摘要，始终注入 system prompt（~800 tokens）
    ↑ 每晚从 Layer 2 重建
Layer 2: 记忆项      — 提取的事实、偏好、事件（可检索）
    ↑ 由 LLM 从 Layer 3 提取
Layer 3: 对话记录    — 原始消息，随时间压缩
```

每晚自动：提取 → 去重 → 重建核心摘要 → 压缩旧对话。

### 心跳循环（Observe → Think → Act）

一个自主后台循环，不是定时任务：

| 阶段 | 做什么 | LLM 调用次数 |
|------|--------|-------------|
| **Observe** | 从所有 Observer 收集环境信息（时间、天气、活动、穿戴设备） | 0 |
| **Think** | LLM 判断：要不要主动联系？（变化检测——只在有变化时触发） | 0–1 |
| **Act** | 发送主动消息、保存观察结果，或者——大多数时候——什么都不做 | 0 |

有限速、很克制。是陪伴，不是骚扰。

### 5 级模型路由

把不同任务路由到最合适的模型——简单任务用便宜/快的，复杂分析才上强模型：

| 层 | 用途 | 举例 |
|----|------|------|
| **LITE** | 便宜/快 | 简单工具任务（打卡、提醒） |
| **CHAT** | 均衡（默认） | 日常对话、主动问候 |
| **DEEP** | 强力 | 代码分析、复杂推理 |
| **BG_FAST** | 便宜后台 | 分类、打标、摘要 |
| **BG_DEEP** | 强力后台 | 心跳推理、记忆操作 |

未配置的层自动回退到 `CHAT_*`。`TIER_ROUTING_ENABLED=false`（默认）时使用原来的双模型（Chat + Think）。

### Observers 与 Skills

| 概念 | 角色 | 举例 |
|------|------|------|
| **Observers** | 被动传感器，为 Think 提供上下文——零 LLM 调用，按间隔节流 | `time_context`、`weather`、`activity_pattern`、`oura`（睡眠/准备度/压力） |
| **Skills** | Chat 模型通过 tool call 调用的主动能力——由 `SKILL.md` + `handler.py` 自动发现 | `memory`、`reminder`、`todo`、`oura` |

两者都**启动时自动发现**——放个文件夹，重启即可。Skills 支持两种 SKILL.md 格式（v1 和 v2），具有丰富的元数据：类型、多轮对话、使用规则和灵活的触发配置。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 快速开始

**前置条件**：Python 3.11+、一个 LLM API key、一个 [Telegram bot token](https://core.telegram.org/bots#how-do-i-create-a-bot)

```bash
git clone https://github.com/mochi-bot/mochibot.git && cd mochibot
cp .env.example .env        # 填入 CHAT_API_KEY、CHAT_MODEL、TELEGRAM_BOT_TOKEN
pip install -r requirements.txt
python -m mochi.main
```

打开 Telegram → 找到你的 bot → 发任意消息。第一个发消息的人自动成为 owner。

两个内置调试命令：`/cost` 查看今日和本月 token 用量，`/heartbeat` 查看最近一次心跳时间和 bot 的决策。

> **任何 OpenAI 兼容 API 都可以。** 设置 `CHAT_BASE_URL` 指向你的服务商：
>
> | 服务商 | `CHAT_BASE_URL` | `CHAT_MODEL` 示例 |
> |--------|-----------------|-------------------|
> | OpenAI（默认） | *（不需要）* | `gpt-4o` |
> | DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
> | Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
> | Ollama（本地） | `http://localhost:11434/v1` | `llama3` |

---

## 部署

心跳持续运行。**如果在笔记本上跑，合盖就离线了。**

| 方案 | 在线时间 | 费用 |
|------|---------|------|
| **云 VM**（Azure、AWS 等） | 7×24 | ~$4–10/月 |
| **树莓派 / 迷你主机** | 7×24（家庭网络） | 一次性 |
| **笔记本** | 开盖时 | 免费 |

> 一台小 VM（1 vCPU、1 GB RAM）绰绰有余——单进程、SQLite、资源占用极低。

---

## 配置

所有配置在 `.env`。核心变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CHAT_PROVIDER` | `openai` | SDK：`openai`（+ 兼容）、`azure_openai`、`anthropic` |
| `CHAT_API_KEY` | — | 你的 API key |
| `CHAT_MODEL` | — | 对话模型（必填） |
| `CHAT_BASE_URL` | — | OpenAI 兼容 API 的自定义端点 |
| `THINK_MODEL` | *=CHAT* | 心跳 + 维护用的便宜模型（可选） |
| `TELEGRAM_BOT_TOKEN` | — | 从 @BotFather 获取 |
| `HEARTBEAT_INTERVAL_MINUTES` | `20` | Observe → Think → Act 循环间隔 |
| `AWAKE_HOUR_START` / `END` | `7` / `23` | 心跳在这些时间外休眠 |
| `MAX_DAILY_PROACTIVE` | `10` | 每日主动消息上限 |
| `TIMEZONE_OFFSET_HOURS` | `0` | 你的 UTC 偏移 |

<details>
<summary>进阶：5 级路由、向量嵌入、集成</summary>

**5 级路由** — 设 `TIER_ROUTING_ENABLED=true`，然后配置每层：

```
TIER_{LITE,CHAT,DEEP,BG_FAST,BG_DEEP}_{PROVIDER,API_KEY,MODEL,BASE_URL}
```

**向量嵌入** — `AZURE_EMBEDDING_ENDPOINT`、`AZURE_EMBEDDING_API_KEY`、`AZURE_EMBEDDING_DEPLOYMENT`

**Oura Ring** — `OURA_CLIENT_ID`、`OURA_CLIENT_SECRET`（运行 `python oura_auth.py` 设置）

完整列表见 [.env.example](.env.example)（~80 个可调参数）。

</details>

**示例** — 双模型省 token：

```dotenv
CHAT_MODEL=gpt-4o            # 聪明模型处理对话
THINK_MODEL=gpt-4o-mini      # 快速模型处理心跳 + 维护
```

---

## 自定义

| 我想改… | 编辑 |
|---------|------|
| 性格、语气、名字 | `prompts/personality.md` |
| 记住哪些内容 | `prompts/memory_extract.md` |
| 什么时候主动联系 | `prompts/think_system.md` |
| 早晚报告 | `prompts/report_morning.md` / `report_evening.md`（默认关闭——在 `.env` 里设 `MORNING_REPORT_HOUR` / `EVENING_REPORT_HOUR` 开启） |
| Observer 间隔 | 各 observer 目录下的 `OBSERVATION.md` |
| 添加新 skill 或 observer | 详见 [CONTRIBUTING.md](CONTRIBUTING.md) |

> **提示**：`prompts/personality.md` 是影响最大的那个文件——它定义了 Mochi 怎么说话（`## Chat`）以及心跳关注什么（`## Think`）。比调任何配置项都重要，先从这里开始。

---

## 架构

详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 路线图

- [x] 支持任意 OpenAI 兼容 API（DeepSeek、Ollama、Groq 等）
- [x] 双模型架构（Chat + Think）
- [x] 5 级模型路由（lite / chat / deep / bg_fast / bg_deep）
- [x] Skill v2 系统 — 丰富的元数据、使用规则、多轮对话、灵活触发
- [x] 扩展 DB schema — 22+ 表、FTS5 全文搜索、可选 sqlite-vec 向量搜索
- [x] 向量嵌入支持 — Azure OpenAI 嵌入 + TTL 缓存
- [ ] 早晚报告（已预埋，在 `.env` 里设 `MORNING_REPORT_HOUR` / `EVENING_REPORT_HOUR` 开启）
- [x] Oura Ring 集成 — 睡眠、准备度、活动、压力（observer + skill）
- [ ] Pre-router — LLM 调用前自动选择 skill
- [ ] 工具治理 — 按 skill 的审批策略、审计日志
- [ ] 管理后台 — 记忆查看、配置、诊断的 Web UI
- [ ] 语音消息支持
- [ ] 多用户支持

## 贡献

详见 [CONTRIBUTING.md](CONTRIBUTING.md)，了解如何添加 skills、observers 和参与框架开发。

## 许可证

MIT — 详见 [LICENSE](LICENSE)

---

<div align="center">

*AI 应该有温度，不只是聪明。*

🍡

</div>
