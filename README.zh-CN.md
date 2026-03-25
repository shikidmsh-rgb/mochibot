<div align="center">

[English](README.md) | [中文](README.zh-CN.md)

# 🍡 MochiBot

**开源 AI 陪伴 bot，带持久记忆和主动问候。**

</div>

---

## 特性

- **轻量** — 单进程、SQLite，不需要 Docker/Redis/Postgres。`pip install` 就能跑
- **持久记忆** — 三层记忆，重启不丢失，每晚自动整理（全文搜索 + 可选向量搜索）
- **主动** — 心跳循环主动找你，而不是干等输入
- **自托管** — 数据留在你自己的机器上
- **易扩展** — 即插即用的 Skills 和 Observers，启动时自动发现
- **省钱** — 5 级模型路由：简单任务用便宜模型，复杂任务才上强模型
- **感知身体** — [Oura Ring](https://ouraring.com) 集成：睡眠、准备度、活动、压力

---

## 设计

### 三层记忆

```
Layer 1: 核心记忆    — 精炼摘要，始终注入 system prompt（~800 tokens）
    ↑ 由 Chat 模型维护（通过工具逐行增删）
Layer 2: 记忆项      — 提取的事实、偏好、事件（可检索，★1/★2/★3 重要度）
    ↑ 由 LLM 从 Layer 3 提取
Layer 3: 对话记录    — 原始消息，随时间压缩
```

8 个记忆工具：保存、搜索、列表、删除（软删除 → 30 天回收站）、编辑核心记忆（逐行增删）、查看核心记忆、统计、回收站。

每晚自动：提取 → 去重 → 过时清理（LLM）→ 重要度调整（升降级）→ 核心记忆审计 → 回收站清理。

### 心跳循环（Observe → Think → Act）

后台循环：

| 阶段 | 做什么 | LLM 调用 |
|------|--------|----------|
| **Observe** | 从 Observer 收集环境信息（时间、天气、活动、穿戴设备） | 0 |
| **Think** | LLM 判断：要不要主动联系？（变化检测——只在有变化时触发） | 0–1 |
| **Act** | 发送主动消息、保存观察结果，或什么都不做 | 0 |

有限速，很克制。

### 5 级模型路由

| 层 | 用途 | 举例 |
|----|------|------|
| **LITE** | 便宜/快 | 工具任务（打卡、提醒） |
| **CHAT** | 均衡（默认） | 对话、主动消息 |
| **DEEP** | 强力 | 代码分析、复杂推理 |
| **BG_FAST** | 便宜后台 | 分类、打标、摘要 |
| **BG_DEEP** | 强力后台 | 心跳推理、记忆操作 |

未配置的层自动回退到 `CHAT_*`。`TIER_ROUTING_ENABLED=false`（默认）时使用双模型（Chat + Think）。

### Pre-Router 与工具治理

按消息动态注入 skill，降低 token 成本：

1. **Pre-Router** — LLM 分类消息并选择要加载的 skill
2. **关键词兜底** — Pre-Router 遗漏时捕获明显需求
3. **工具升级** — LLM 对话中途可通过 `request_tools` 请求缺少的 skill

工具策略层对每次调用做 check / filter / 限速。

### 日记（工作记忆）

Chat 和 Think 共享的每日便签——观察、笔记和不适合写入长期记忆的上下文。每晚自动归档。

### Observers 与 Skills

| 概念 | 角色 | 举例 |
|------|------|------|
| **Observers** | 被动传感器，为 Think 提供上下文——零 LLM 调用，按间隔节流 | `time_context`、`weather`、`activity_pattern`、`oura` |
| **Skills** | Chat 模型通过 tool call 调用的能力——由 `SKILL.md` + `handler.py` 自动发现 | `memory`、`reminder`、`todo`、`diary`、`oura` |

两者启动时自动发现——放个文件夹，重启即可。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

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

调试命令：`/cost`（token 用量）、`/heartbeat`（最近心跳状态）。

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

心跳持续运行——笔记本合盖就离线。

| 方案 | 在线时间 | 费用 |
|------|---------|------|
| **云 VM**（Azure、AWS 等） | 7×24 | ~$4–10/月 |
| **树莓派 / 迷你主机** | 7×24（家庭网络） | 一次性 |
| **笔记本** | 开盖时 | 免费 |

> 一台小 VM（1 vCPU、1 GB RAM）足够——单进程、SQLite、资源占用极低。

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
<summary>进阶：5 级路由、Pre-Router、向量嵌入、集成</summary>

**5 级路由** — 设 `TIER_ROUTING_ENABLED=true`，然后配置每层：

```
TIER_{LITE,CHAT,DEEP,BG_FAST,BG_DEEP}_{PROVIDER,API_KEY,MODEL,BASE_URL}
```

**Pre-Router** — `TOOL_ROUTER_ENABLED=true` 启用基于 LLM 的 skill 自动选择。`TOOL_ESCALATION_ENABLED=true`（默认）允许对话中途请求新 skill。

**向量嵌入** — `AZURE_EMBEDDING_ENDPOINT`、`AZURE_EMBEDDING_API_KEY`、`AZURE_EMBEDDING_DEPLOYMENT`

**Oura Ring** — `OURA_CLIENT_ID`、`OURA_CLIENT_SECRET`（运行 `python oura_auth.py` 设置）

完整列表见 [.env.example](.env.example)（关键参数）；详见 `mochi/config.py`（~70 个可调参数）。

</details>

**示例** — 双模型配置：

```dotenv
CHAT_MODEL=gpt-4o            # 对话
THINK_MODEL=gpt-4o-mini      # 心跳 + 维护
```

---

## 自定义

| 我想改… | 编辑 |
|---------|------|
| 性格、语气、名字 | `prompts/personality.md` |
| 记住哪些内容 | `prompts/memory_extract.md` |
| 什么时候主动联系 | `prompts/think_system.md` |
| 早晚报告 | `prompts/report_morning.md` / `report_evening.md`（默认关闭——设 `MORNING_REPORT_HOUR` / `EVENING_REPORT_HOUR` 开启） |
| Observer 间隔 | 各 observer 目录下的 `OBSERVATION.md` |
| 添加 skill 或 observer | 详见 [CONTRIBUTING.md](CONTRIBUTING.md) |

> `prompts/personality.md` 是影响最大的文件——定义 bot 的说话方式和心跳关注点。

---

## 架构

详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 路线图

- [x] 任意 OpenAI 兼容 API（DeepSeek、Ollama、Groq 等）
- [x] 双模型架构（Chat + Think）
- [x] 5 级模型路由
- [x] Skill v2 — 丰富元数据、使用规则、多轮、灵活触发
- [x] 扩展 DB — 22+ 表、FTS5、可选 sqlite-vec
- [x] 向量嵌入（Azure OpenAI + TTL 缓存）
- [x] Oura Ring 集成（observer + skill）
- [x] Pre-router — 自动选择 skill
- [x] 工具治理 — 策略检查、过滤、限速
- [x] 日记系统 — 每日工作记忆 + 夜间归档
- [x] 夜间维护 — 去重、LLM 过时清理、重要度调整、核心记忆审计、回收站清理
- [x] 模块化 Prompt 组装
- [x] 打字节奏 — 多气泡 + 打字指示器
- [x] 早晚报告
- [ ] 管理后台（Web UI）
- [ ] 语音消息
- [ ] 多用户支持

## 贡献

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT — 详见 [LICENSE](LICENSE)
