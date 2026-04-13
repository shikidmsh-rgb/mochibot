<div align="center">

# 🍡 MochiBot

**一个有记忆、会主动找你、能催你吃药的 AI 陪伴 bot。**

轻量自托管 · SQLite · 支持 OpenAI / Azure / Anthropic

</div>
<img width="1110" height="544" alt="image" src="https://github.com/user-attachments/assets/3e81dc4d-b517-43a9-ac37-9899b73e5fea" />

---

## 它能做什么

### 🧠 长期记忆

- **核心记忆**——你的偏好、你们的关系，每次对话都在
- **记忆项**——自动从对话中提取，按重要度分级，全文搜索 + 向量搜索
- **每天一本 diary**——习惯进度、待办、提醒汇总成当日状态面板
- **夜间自动整理**——去重、清过时的、调重要度，不会越存越乱


### 💬 活人感

- **自定义性格**——性格、语气、关注点都写在一个 prompt 文件里，你定义它是谁
- **表情包**——转发 Telegram 表情包给ta，自动学习。之后聊天会自己发
- **后台心跳**——不等你发消息。ta在后台定期看你的习惯、待办、提醒，该催的时候主动找你
- **跟你一起作息**——你睡ta也睡，你醒ta也醒
- **打字节奏**——消息拆成多条气泡 + 打字指示器
- **早安晚安**——早上告诉你今天有什么，晚上复盘今天怎么样（可选）

### ✅ ADHD 友好

- **习惯追踪**——频率（每天两次、周一三五……）、时间上下文（早晚、饭后）、重要度（⚡ = 健康/用药类）。打卡、暂停都行
- **到点就催**——⚡重要习惯过时了必催，不是看心情。晚上药没吃？它不会放过你
- **精确提醒**——到点即响，支持循环（每天/工作日/每周/每月）
- **待办清单**——随口说"我要买菜"就记下来，快到期的会推给你
- **打卡历史**——`✅ ✅ ❌ ✅ ✅ ✅ ✅`

### 🍱 健康追踪

- **饮食记录**——"午饭吃了米饭炒青菜鸡腿" → `~520kcal（蛋白质 35g / 碳水 45g / 脂肪 12g）`。按天/周查历史
- **Oura Ring**——睡眠、准备度、活动、压力数据接入，纳入心跳上下文

### 🔍 信息搜索

- **联网搜索**——问当前事件、新闻、价格，自动用 DuckDuckGo 搜索并总结（无需 API key）
- **天气查询**——配置城市后自动获取天气，心跳中也会带天气上下文

### 💰 省钱

- **Pre-Router**——按消息动态选择需要的能力，不带用不上的工具，省 token
- **3 级模型路由**——发表情用便宜模型，复杂分析才上强模型
- 整套路由默认关闭，零配置就是一个 chat + 一个 think 模型，够用就行

---

## 还有

- **轻量**——单进程、SQLite，不需要 Docker/Redis/Postgres，`pip install` 就能跑
- **自托管**——数据留在你自己的机器上
- **易扩展**——Skill 和 Observer 即插即用，放个文件夹重启就行
- **管理后台**——Web UI 配置模型、调心跳参数、开关 skill、编辑人设 prompt
- **目前只支持 Telegram**——Transport 层是抽象接口，可以自己扩展其他平台

---

## 快速开始

**准备好**：Python 3.11+、一个 LLM API key、一个 [Telegram bot token](https://core.telegram.org/bots#how-do-i-create-a-bot)

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git
cd mochibot
```

- **Windows**：双击 `setup.bat`
- **macOS / Linux**：`bash setup.sh`

脚本会自动搞定环境和依赖，然后打开管理后台。在浏览器里配好 API key、模型、Telegram token，点 **「启动 Bot」** 就行了。

第一个给 bot 发消息的人自动成为 owner。

> **支持三种 API 提供商：**
>
> | 提供商 | `CHAT_PROVIDER` | `CHAT_BASE_URL` | `CHAT_MODEL` 示例 |
> |--------|-----------------|-----------------|-------------------|
> | OpenAI（默认） | `openai` | *（不需要）* | `gpt-4o` |
> | DeepSeek | `openai` | `https://api.deepseek.com/v1` | `deepseek-chat` |
> | Groq | `openai` | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
> | Ollama（本地） | `openai` | `http://localhost:11434/v1` | `llama3` |
> | Azure OpenAI | `azure_openai` | *（你的 Azure 端点）* | `gpt-4o` |
> | Anthropic Claude | `anthropic` | *（不需要）* | `claude-sonnet-4-20250514` |

---

## 部署

心跳持续运行——笔记本合盖就离线。

| 方案 | 在线时间 | 费用 |
|------|---------|------|
| **云 VM**（Azure、AWS 等） | 7×24 | ~$4–10/月 |
| **树莓派 / 迷你主机** | 7×24（家庭网络） | 一次性 |
| **笔记本** | 开盖时 | 免费 |

> 一台小 VM（1 vCPU、1 GB RAM）足够——单进程、SQLite、资源占用极低。

### Docker 部署（推荐）

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git && cd mochibot
cp .env.example .env        # 填写 CHAT_API_KEY、CHAT_MODEL、TELEGRAM_BOT_TOKEN
docker compose up -d         # 后台运行
```

查看日志：`docker compose logs -f`

更新：`git pull && docker compose up -d --build`

数据保存在 `data/` 目录，容器删除不丢失。

### 无 Docker 部署（systemd）

<details>
<summary>展开步骤</summary>

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git && cd mochibot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 填写必要配置
```

创建 systemd 服务：

```bash
sudo tee /etc/systemd/system/mochibot.service << 'EOF'
[Unit]
Description=MochiBot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/mochibot
ExecStart=/path/to/mochibot/venv/bin/python -m mochi.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

```bash
sudo systemctl enable --now mochibot    # 启动 + 开机自启
sudo journalctl -u mochibot -f          # 查看日志
```

</details>

### 云服务器上使用管理后台

管理后台默认绑定 `127.0.0.1:8080`，只能从服务器本机访问。部署在云 VM 上时，推荐用以下方式远程打开后台：

#### 方式一：SSH 隧道（推荐，零配置）

在你**本地电脑**的终端运行：

```bash
ssh -L 8080:127.0.0.1:8080 user@your-server-ip
```

然后在本地浏览器打开 `http://localhost:8080`。流量通过 SSH 加密传输，服务端不需要改任何配置。

#### 方式二：反向代理 + HTTPS（长期使用）

适合需要频繁访问、或多人管理的场景。用 Caddy / Nginx 做反向代理，让它处理 HTTPS：

**Caddy**（自动申请证书，最简单）：

```
admin.yourdomain.com {
    reverse_proxy 127.0.0.1:8080
}
```

**Nginx**：

```nginx
server {
    listen 443 ssl;
    server_name admin.yourdomain.com;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

> **重要**：使用反向代理时，务必在 `.env` 中设置 `ADMIN_TOKEN`，否则任何人都能访问你的后台。

#### 不推荐：直接暴露

设置 `ADMIN_BIND=0.0.0.0` 可以让后台对外开放，但因为是明文 HTTP，token 会在网络上裸传。**仅限内网或测试环境使用。**

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
| `HEARTBEAT_INTERVAL_MINUTES` | `20` | 心跳循环间隔 |
| `AWAKE_HOUR_START` / `END` | `7` / `23` | 心跳在这些时间外休眠 |
| `MAX_DAILY_PROACTIVE` | `10` | 每日主动消息上限 |
| `TIMEZONE_OFFSET_HOURS` | `8` | 你的 UTC 偏移 |

<details>
<summary>进阶：3 级路由、Pre-Router、向量嵌入、集成</summary>

**3 级路由** — 通过 `.env` 或管理面板为每层配置不同模型（DB 配置优先于 `.env`）：

```
TIER_{LITE,CHAT,DEEP}_{PROVIDER,API_KEY,MODEL,BASE_URL}
```

**Pre-Router** — `TOOL_ROUTER_ENABLED=true` 启用基于 LLM 的 skill 自动选择。`TOOL_ESCALATION_ENABLED=true`（默认）允许对话中途请求缺少的 skill。

**向量嵌入** — `EMBEDDING_PROVIDER`（openai / azure_openai / ollama / none）、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`。配置后记忆检索从纯关键词升级为语义搜索。安装 `pip install sqlite-vec` 可启用原生向量 KNN，速度更快；不装也能跑（退化为 Python 计算余弦相似度）。

**Oura Ring** — `OURA_CLIENT_ID`、`OURA_CLIENT_SECRET`（运行 `python oura_auth.py` 设置）

完整列表见 [.env.example](.env.example)（关键参数）；详见 `mochi/config.py`（~80 个可调参数）。

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
| 性格、语气、名字 | `prompts/system_chat/soul.md` |
| 记住哪些内容 | `prompts/memory_extract.md` |
| 什么时候主动联系、催促哪些习惯 | `prompts/think_system.md` |
| 早间汇报 | `prompts/think_system.md`（Think 模型在每天第一次心跳时自动生成早报，无需额外配置） |
| 添加 skill 或 observer | 详见 [CONTRIBUTING.md](CONTRIBUTING.md) |

> 性格文件影响最大——改了它，bot 说话方式就变。

---

## 路线图

- [x] 多 API 提供商（OpenAI 兼容 / Azure OpenAI / Anthropic）
- [x] 双模型架构（Chat + Think）
- [x] 3 级模型路由 + Pre-Router
- [x] 持久记忆（三层 + 8 工具 + 夜间维护）
- [x] 习惯追踪（频率/重要度/上下文/暂停/延后 + 心跳催促）
- [x] 精确提醒（到点触发 + 循环提醒）
- [x] 饮食记录（自然语言 → 热量估算 + 历史查询）
- [x] 联网搜索（DuckDuckGo，无需 API key）
- [x] Oura Ring 集成
- [x] 日记系统（今日状态面板 + 夜间归档）
- [x] 管理后台（Web UI）
- [x] 打字节奏（多气泡 + 打字指示器）
- [x] 早间汇报（Think 驱动）
- [ ] 语音消息
- [ ] 多用户支持

## 贡献

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT — 详见 [LICENSE](LICENSE)
