<div align="center">

# 🍡 MochiBot

**一个有记忆、会主动找你、能催你吃药的 AI 陪伴 bot。**

轻量自托管 · SQLite · 支持 OpenAI / Azure / Anthropic

**零门槛配置**——运行脚本后自动打开管理后台（Web UI），在浏览器里填 API key 和 bot token，点一下就能启动。云服务器？只填 bot token，发 `/admin` 给 bot 就能在手机上完成全部配置。

</div>
<img width="1110" height="544" alt="image" src="https://github.com/user-attachments/assets/3e81dc4d-b517-43a9-ac37-9899b73e5fea" />

---

## TA 能做什么

### 🧠 长期记忆

- **核心记忆**——你的偏好、你们的关系，每次对话都在
- **记忆项**——自动从对话中提取，按重要度分级，全文搜索 + 向量搜索
- **知识图谱**——自动学习你世界里的人、宠物、地点和它们之间的关系，聊到时精准注入上下文
- **每天一本 diary**——习惯进度、待办、提醒汇总成当日状态面板
- **夜间自动整理**——去重、清过时的、调重要度，不会越存越乱


### 💬 活人感

- **自定义性格**——性格、语气、关注点都写在一个 prompt 文件里，你定义 TA 是谁
- **表情包**——转发 Telegram 表情包给 TA，自动学习。之后聊天会自己发
- **后台心跳**——不等你发消息。TA 在后台定期看你的习惯、待办、提醒，该催的时候主动找你
- **跟你一起作息**——你睡 TA 也睡，你醒 TA 也醒
- **打字节奏**——消息拆成多条气泡 + 打字指示器


### ✅ ADHD 友好

- **习惯追踪**——频率（每天两次、周一三五……）、时间上下文（早晚、饭后）、重要度（⚡ = 健康/用药类）。打卡、暂停都行
- **到点就催**——⚡重要习惯过时了必催，不是看心情。晚上药没吃？TA 不会放过你
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
- **管理后台**——Web UI 配置模型、调心跳参数、开关 skill、编辑人设 prompt。手机适配，支持 `/admin` 命令远程获取管理链接。云服务器上只填一个 bot token 就能启动（Setup Mode），在手机上完成全部配置
- **支持 Telegram 和 WeChat**——二选一，在管理后台配置。推荐 Telegram（支持表情包、语音等丰富交互）

---

## 已支持的 Skill

所有 Skill **即插即用**——在管理后台一键开关，或直接在 `mochi/skills/` 下添加/删除文件夹，重启即生效。不需要改主流程代码。

| Skill | 说明 |
|-------|------|
| **habit** | 习惯追踪——打卡、暂停、催促，支持频率和重要度 |
| **todo** | 一次性待办——追踪到完成为止 |
| **note** | 备忘/笔记——不定时、不打卡，持续关注或条件触发的事项，心跳每轮可见 |
| **reminder** | 定时提醒——到点触发，支持重复（每天/工作日/每周/每月） |
| **meal** | 饮食记录——自然语言 → 热量估算 + 营养素拆解 + 历史查询 |
| **oura** | Oura Ring 集成——睡眠、活动、准备度、压力、心率、血氧（需配置） |
| **weather** | 天气查询——通过 wttr.in 获取，心跳中自动带入上下文 |
| **web_search** | 联网搜索——DuckDuckGo，无需 API key |
| **sticker** | 表情包——学习你转发的贴纸，聊天时自动发（仅 Telegram） |

> **想加一个新 Skill？** 在 `mochi/skills/` 下新建文件夹，放入 `SKILL.md` + `handler.py`，重启 bot 就会自动注册。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 命令

以下斜杠命令在 Telegram 和 WeChat 中均可使用（除 `/help` 外均为 owner 专用）：

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助信息和可用命令列表 |
| `/heartbeat` | 查看心跳状态——系统运行情况、主动消息计数、上次心跳摘要 |
| `/cost` | Token 用量统计——今日 / 本月用量，按模型分类 |
| `/notes` | 查看备忘笔记 |
| `/diary` | 查看今日日记——今日状态面板 + 日记内容 |
| `/admin` | 获取管理后台链接（带 token，可在手机浏览器打开） |
| `/restart` | 重启 Bot 进程 |

---

## 快速开始

> **完全新手？** 看 [新手上路手册](docs/getting-started.md)，手把手从打开终端开始教你。

**准备好**：Python 3.11+、一个 LLM API key、一个消息平台（[Telegram bot token](https://core.telegram.org/bots#how-do-i-create-a-bot) 或 WeChat）

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git
cd mochibot
```

- **Windows**：双击 `setup.bat`
- **macOS / Linux**：`bash setup.sh`

脚本会自动搞定环境和依赖，然后打开管理后台。在浏览器里配好 API key、模型、消息平台（Telegram 或 WeChat），点 **「启动 Bot」** 就行了。

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
cp .env.example .env        # 填写 CHAT_API_KEY、CHAT_MODEL，以及 TELEGRAM_BOT_TOKEN 或 WEIXIN_ENABLED
docker compose up -d         # 后台运行
```

查看日志：`docker compose logs -f`

更新：`git pull && docker compose up -d --build`

数据保存在 `data/` 目录，容器删除不丢失。

### 手动运行（无 Docker / 无 systemd）

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git && cd mochibot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 填写必要配置
python start.py
```

`start.py` 会在 bot 请求重启时（如通过管理后台的重启按钮）自动重新启动进程。如果直接运行 `python -m mochi.main`，重启按钮将不会生效。

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

配置了消息平台（Telegram / WeChat）后，管理后台会自动绑定到 `0.0.0.0` 并生成 `ADMIN_TOKEN`，无需手动配置。

#### 方式一：`/admin` 命令（最简单）

给 bot 发 `/admin`，TA 会回复管理后台的 URL（带 token）。在手机浏览器里打开就能配置。

> 同一局域网下直接可用。云服务器需要确保端口（默认 8080）在安全组/防火墙中放开。

#### 方式二：SSH 隧道（安全远程访问）

在你**本地电脑**的终端运行：

```bash
ssh -L 8080:localhost:8080 user@your-server-ip
```

然后在本地浏览器打开 `http://localhost:8080`。流量通过 SSH 加密传输。

#### 方式三：反向代理 + HTTPS（长期使用）

适合需要频繁访问、或多人管理的场景。用 Caddy / Nginx 做反向代理，处理 HTTPS：

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

> **重要**：使用反向代理时，确保 `.env` 中有 `ADMIN_TOKEN`（自动生成或手动设置），否则任何人都能访问你的后台。

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
| `TELEGRAM_BOT_TOKEN` | — | 从 @BotFather 获取（Telegram 平台） |
| `WEIXIN_ENABLED` | `false` | 启用 WeChat 平台（与 Telegram 二选一） |
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

**向量嵌入** — `EMBEDDING_PROVIDER`（openai / azure_openai / ollama / none）、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`。配置后记忆检索从纯关键词升级为语义搜索，并通过 `sqlite-vec`（已包含在依赖中）实现原生向量 KNN 加速。

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

> 性格文件影响最大——改了之后，bot 说话方式就变。

---

## 路线图

- [x] 多 API 提供商（OpenAI 兼容 / Azure OpenAI / Anthropic）
- [x] 双模型架构（Chat + Think）
- [x] 3 级模型路由 + Pre-Router
- [x] 持久记忆（三层 + 8 工具 + 夜间维护）
- [x] 知识图谱（实体关系自动提取 + 对话注入）
- [x] 习惯追踪（频率/重要度/上下文/暂停/延后 + 心跳催促）
- [x] 精确提醒（到点触发 + 循环提醒）
- [x] 饮食记录（自然语言 → 热量估算 + 历史查询）
- [x] 联网搜索（DuckDuckGo，无需 API key）
- [x] Oura Ring 集成
- [x] 日记系统（今日状态面板 + 夜间归档）
- [x] 管理后台（Web UI）
- [x] Setup Mode + `/admin` 命令（手机配置）
- [x] 打字节奏（多气泡 + 打字指示器）
- [x] 早间汇报（Think 驱动）
- [ ] 语音消息
- [ ] 多用户支持

## 贡献

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT — 详见 [LICENSE](LICENSE)
