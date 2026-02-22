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
- **持久记忆** — 三层记忆系统，重启不丢失，每晚自动整理
- **主动关心** — 心跳循环主动找你聊，而不是干等你输入
- **完全私有** — 自托管，数据永远不离开你的机器
- **易扩展** — 即插即用的 Skills 和 Observers，启动时自动发现

> 🧪 **这只是 MVP 版本。** 真正的魔法从接入可穿戴设备开始——比如 [Oura Ring](https://ouraring.com)。心跳随你醒来而跳动，随你入睡而安静，在你说不出口的时候，身体数据替你开口。

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

### 双模型架构

MochiBot 把 **Chat**（对话）和 **Think**（心跳 + 维护）分开——可以跑不同的模型，也可以用同一个：

```
┌──────────────────────────────────────────────────┐
│                                                  │
│  Chat Model              Think Model             │
│  ┌──────────────┐        ┌──────────────┐        │
│  │ 对话          │        │ 心跳循环      │        │
│  │ 工具调用      │        │ 记忆维护      │        │
│  │ 记忆检索      │        │ 记忆去重      │        │
│  └──────────────┘        └──────────────┘        │
│         ▲                       ▲                │
│    CHAT_MODEL              THINK_MODEL           │
│    （必填）            （可选——默认使用            │
│                        CHAT_MODEL）              │
│                                                  │
└──────────────────────────────────────────────────┘
```

**为什么要分开？** 心跳每 N 分钟跑一次，记忆维护每晚跑一次。这些任务比对话简单——用便宜的模型就够了，能大幅降低 API 费用。

### Observers 与 Skills

| 概念 | 角色 | 举例 |
|------|------|------|
| **Observers** | 被动传感器，为 Think 提供上下文——零 LLM 调用，按间隔节流 | `time_context`、`weather`、`activity_pattern`、自定义穿戴设备 |
| **Skills** | Chat 模型通过 tool call 调用的主动能力 | `memory`、`reminder`、`todo`、`web_search` |

两者都**启动时自动发现**——放个文件夹，重启即可。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

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
| `THINK_PROVIDER` | *=CHAT* | Think 的独立服务商（可选） |
| `TELEGRAM_BOT_TOKEN` | — | 从 @BotFather 获取 |
| `HEARTBEAT_INTERVAL_MINUTES` | `20` | Observe → Think → Act 循环间隔 |
| `AWAKE_HOUR_START` / `END` | `7` / `23` | 心跳在这些时间外休眠 |
| `MAX_DAILY_PROACTIVE` | `10` | 每日主动消息上限 |
| `MAINTENANCE_HOUR` | `3` | 每晚维护时间（本地时间） |
| `TIMEZONE_OFFSET_HOURS` | `0` | 你的 UTC 偏移 |

完整列表见 [.env.example](.env.example)。

**双模型示例** — 用便宜的 Think 模型省 token：

```dotenv
CHAT_MODEL=gpt-4o            # 聪明模型处理对话
THINK_MODEL=gpt-4o-mini      # 快速模型处理心跳 + 维护
```

Chat 和 Think 甚至可以用**不同的服务商**——比如 Chat 用强模型，Think 用便宜的：

```dotenv
CHAT_PROVIDER=anthropic
CHAT_API_KEY=sk-ant-...
CHAT_MODEL=claude-sonnet-4-20250514

THINK_PROVIDER=openai          # 任何 OpenAI 兼容 API
THINK_BASE_URL=https://api.groq.com/openai/v1
THINK_API_KEY=your-groq-key
THINK_MODEL=llama-3.3-70b-versatile
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

> `prompts/personality.md` 是影响最大的那个文件——它定义了 Mochi 怎么说话（`## Chat`）以及心跳关注什么（`## Think`）。

---

## 最佳实践

- **部署到 VM** — 心跳需要 7×24 在线才能成为真正的陪伴
- **接入可穿戴设备** — [Oura Ring](https://ouraring.com)（或类似设备）把睡眠/准备度/活动数据接入心跳，是体验提升最大的一步。为你的设备写一个自定义 observer 即可
- **用便宜的 Think 模型** — 心跳和维护不需要你最聪明的模型（见[双模型架构](#双模型架构)）
- **从 `prompts/personality.md` 开始** — 定制 bot 的声音比任何配置项都重要
- **先用内置 observer** — time、activity、weather 提供了不错的基线

---

## 架构

详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

```
┌─────────────────────────────┐
│ L1: Identity (prompts)      │  ← Bot 的人格
├─────────────────────────────┤
│ L2: Config (.env)           │  ← 可调参数
├─────────────────────────────┤
│ L3: Skills + Observers      │  ← 能力 + 传感器
├─────────────────────────────┤
│ L4: Core (orchestration)    │  ← 框架
└─────────────────────────────┘
```

## 路线图

- [x] 支持任意 OpenAI 兼容 API（DeepSeek、Ollama、Groq 等）
- [x] 双模型架构（Chat + Think）
- [ ] 工具治理 — 按 skill 的审批策略、审计日志
- [ ] 硬件集成 — Oura Ring observer（睡眠、准备度、活动）
- [ ] 管理后台 — 记忆查看、配置、诊断的 Web UI

## 贡献

详见 [CONTRIBUTING.md](CONTRIBUTING.md)，了解如何添加 skills、observers 和参与框架开发。

## 许可证

MIT — 详见 [LICENSE](LICENSE)

---

<div align="center">

*AI 应该有温度，不只是聪明。*

🍡

</div>
