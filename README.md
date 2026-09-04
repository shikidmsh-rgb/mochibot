<div align="center">

# 🍡 MochiBot

**一个有记忆、会主动找你、能催你吃药的 AI 陪伴 bot。**

轻量自托管 · SQLite · 支持 OpenAI / DeepSeek / Anthropic / Gemini / OpenAI-compatible

**零门槛配置**——运行脚本后自动打开管理后台（Web UI），在浏览器里填 API key 和 bot token，点一下就能启动。

</div>
<img width="1319" height="1261" alt="image" src="https://github.com/user-attachments/assets/4b5b2abe-e07a-465c-9e39-0ad83c5adb69" />



---

## TA 能做什么

### 🧠 长期记忆

- **Core**——一份可读、可编辑的自由文本，每次对话都在；旧 Core、人格文件和 Notes 会安全迁移到 `data/core.md`
- **对话摘要**——每 20 个完整回合在后台连续续写；失败期间仍保留旧摘要和全部未摘要原文
- **记忆项**——每 10 个普通聊天回合由无人格 Lite 连续提取；全文召回常开，向量仅作可选增强
- **知识图谱**——从有来源的记忆项增量投影实体关系，聊到时可选注入精确上下文
- **每天一本 diary**——习惯进度、待办、提醒汇总成当日状态面板
- **每周记忆整理**——Nightly 只可靠归档；每周由同一个 Main 回看真实证据，谨慎整理 Core 与记忆项


### 💬 活人感

- **自定义性格**——性格、语气、关系与重要经历都写在一份自由文本 Core 里，你和 TA 可以持续一起修改
- **表情包**——转发 Telegram 表情包给 TA，自动学习。之后聊天会自己发
- **后台心跳**——不等你发消息。TA 会有自由陪伴时间，也会在生活事实变化时自己决定是否行动或开口
- **按需看看近况**——Main 可以读取 Observer 已有缓存的安全视图，不触发实时采集，也不会看到原始插件数据
- **跟你一起作息**——你睡 TA 也睡，你醒 TA 也醒
- **打字节奏**——消息拆成多条气泡 + 打字指示器


### ✅ ADHD 友好

- **习惯追踪**——频率（每天两次、周一三五……）、时间上下文（早晚、饭后）、重要度（⚡ = 健康/用药类）。打卡、暂停都行
- **到点就催**——⚡重要习惯过时了必催，不是看心情。晚上药没吃？TA 不会放过你
- **精确提醒**——到点即响，支持循环（每天/工作日/每周/每月）
- **待办清单**——随口说"我要买菜"就记下来，快到期的会推给你
- **打卡历史**——`✅ ✅ ❌ ✅ ✅ ✅ ✅`

### 🍱 饮食追踪

- **饮食记录**——"午饭吃了米饭炒青菜鸡腿" → `~520kcal（蛋白质 35g / 碳水 45g / 脂肪 12g）`。按天/周查历史

### 🔍 信息搜索

- **联网搜索**——问当前事件、新闻、价格，自动用 DuckDuckGo 搜索并总结（无需 API key）
- **天气查询**——配置城市后自动获取天气，心跳中也会带天气上下文

### 💰 省钱

- **Pre-Router**——按消息动态选择需要的能力，不带用不上的工具，省 token
- **Main + Lite**——Main 保持陪伴人格；Lite 只做分类、提取和校验
- Pre-Router 默认开启；未明确分配 Lite 时会跳过分类，不会偷偷让 Main 代班

---

## 还有

- **轻量**——单进程、SQLite，不需要 Docker/Redis/Postgres，`pip install` 就能跑
- **自托管**——聊天记录、记忆等持久化数据留在自己的机器上；对话内容、图片和搜索请求仍会发送给对应的外部服务
- **易扩展**——Skill 和 Observer 即插即用，放个文件夹重启就行
- **管理后台**——Web UI 配置模型、调心跳参数、开关 skill、编辑人设 prompt。本地部署时自动打开浏览器；云服务器需通过 SSH 隧道或反向代理访问（详见下方部署章节）
- **ChatGPT 搬家**——上传 ChatGPT 导出的 JSON，LLM 自动提取 Core 草稿和记忆条目，预览编辑后一键导入 MochiBot
- **支持 Telegram 和 WeChat**——二选一，在管理后台配置。推荐 Telegram（支持图片和表情包；语音暂未支持）；微信目前只处理文字消息

---

## 已支持的 Skill

所有 Skill **即插即用**——在管理后台一键开关，或直接在 `mochi/skills/` 下添加/删除文件夹，重启即生效。不需要改主流程代码。

| Skill | 说明 |
|-------|------|
| **habit** | 习惯追踪——打卡、暂停、催促，支持频率和重要度 |
| **todo** | 一次性待办——追踪到完成为止 |
| **reminder** | 定时提醒——到点触发，支持重复（每天/工作日/每周/每月） |
| **meal** | 饮食记录——自然语言 → 热量估算 + 营养素拆解 + 历史查询 |
| **weather** | 天气查询——通过 wttr.in 获取，心跳中自动带入上下文 |
| **web_search** | 联网搜索——DuckDuckGo，无需 API key |
| **sticker** | 表情包——学习你转发的贴纸，聊天时自动发（仅 Telegram） |
| **skill_management** | 技能管理——通过对话列出、开关、配置所有技能 |

> **想加一个新 Skill？** 在 `mochi/skills/` 下新建文件夹，放入 `SKILL.md` + `handler.py`，重启 bot 就会自动注册。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 命令

以下斜杠命令在 Telegram 和 WeChat 中均可使用（除 `/help` 外均为 owner 专用）：

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助信息和可用命令列表 |
| `/heartbeat` | 查看心跳状态——系统运行情况、主动消息计数、上次心跳摘要 |
| `/cost` | Token 用量统计——今日 / 本月用量，按模型分类 |
| `/core` | 查看 Core |
| `/diary` | 查看今日日记——今日状态面板 + 日记内容 |
| `/admin` | 获取管理后台链接（带 token，可在手机浏览器打开） |
| `/skilloff` | 切换到闲聊模式——关闭非核心 skill 和 prerouter，省 token |
| `/skillon` | 恢复完整模式——重新启用所有 skill |
| `/reset` | 重置对话上下文——后续聊天 LLM 看不到之前的 history（DB 保留，长期记忆不影响） |
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

第一个给 bot 发消息的人会自动成为 owner。部署后请先由自己发送第一条消息，避免公开 Bot 被他人抢先认领。

使用 Telegram 时，可以直接发送单张图片让 Bot 查看；支持 OpenAI、DeepSeek、
Anthropic 和 Gemini，具体的 Main 模型需要具备图片理解能力。图片仅用于
当前一轮对话，不会保存到本地。微信通道目前只处理文字消息。

> **内置四类 API 预设，并支持 OpenAI-compatible 接口：**
>
> | 提供商 | 适配器 | `MAIN_BASE_URL` | `MAIN_MODEL` 示例 |
> |--------|-----------------|-----------------|-------------------|
> | OpenAI（默认） | `openai` | *（不需要）* | `gpt-4o` |
> | DeepSeek | `openai` | `https://api.deepseek.com/v1` | `deepseek-chat` |
> | Anthropic Claude | `anthropic` | *（不需要）* | `claude-sonnet-4-20250514` |
> | Google Gemini | `openai` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash` |
> | OpenAI-compatible | `openai` | 服务方提供的 HTTPS API 根地址 | 服务方提供的模型 ID |
>
> 兼容接口由服务方实现。MochiBot 只按 OpenAI Chat Completions 协议发送请求，
> 不保证所有中转服务、模型能力或参数均可用；请使用管理后台的「测试」确认。

---

## 部署

心跳持续运行——笔记本合盖就离线。

| 方案 | 在线时间 | 费用 |
|------|---------|------|
| **云 VM**（Azure、AWS 等） | 7×24 | ~$4–10/月 |
| **树莓派 / 迷你主机** | 7×24（家庭网络） | 一次性 |
| **笔记本** | 开盖时 | 免费 |

> 一台小 VM（1 vCPU、1 GB RAM）足够——单进程、SQLite、资源占用极低。

> **⚠️ 境内 vs 境外服务器**
>
> | | 境内（阿里云、腾讯云等） | 境外（AWS、Azure、GCP 等） |
> |---|---|---|
> | **Telegram** | ❌ 无法访问 | ✅ |
> | **国外 AI API**（OpenAI、Anthropic、Gemini） | ❌ 无法直接调用 | ✅ |
> | **WeChat** | ✅ | ✅ |
> | **DeepSeek** | ✅ | ✅ |
>
> 如果你用 **Telegram + 国外 AI**，选境外服务器最省心。境内服务器可使用
> **WeChat + 国内模型**，也可以自行配置可访问的 OpenAI-compatible 接口。

### Docker 部署（推荐）

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git && cd mochibot
cp .env.example .env        # 填写 MAIN_API_KEY、MAIN_MODEL，以及 TELEGRAM_BOT_TOKEN 或 WEIXIN_ENABLED
docker compose up -d         # 后台运行
```

查看日志：`docker compose logs -f`

更新：`git pull && docker compose up -d --build`（你的 `data/` 和 `.env` 不受影响）

数据保存在 `data/` 目录，容器删除不丢失。
通过管理后台修改的 prompt 会写到 `data/prompts/`，也会随这个卷一起持久化。

> **安全提示**：`data/` 目录包含数据库（含聊天记录、记忆等敏感信息），`.env` 包含 Bot Token、同步的模型 API Key 等明文配置。设置了 `ADMIN_TOKEN` 时，数据库里的模型 API Key 会加密；聊天和记忆不会额外加密。请限制这些文件的系统访问权限，并建议开启磁盘加密（如 BitLocker / FileVault / LUKS）。

### 手动运行（无 Docker / 无 systemd）

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git && cd mochibot
```

- **Windows**：双击 `setup.bat`
- **macOS / Linux**：`bash setup.sh`

脚本会自动创建 `.venv`、安装依赖，然后启动管理后台。

#### 更新

**Windows 用户**（推荐）：双击项目目录里的 `update.bat`，按提示操作即可。
脚本会自动：拉最新代码 → 装新依赖 → 启动 bot。

**macOS / Linux 用户**：

```bash
cd mochibot
source .venv/bin/activate
git pull
pip install -r requirements.txt
```

然后重新跑 `bash setup.sh` 启动 bot。

<details>
<summary>Windows 用户偏好命令行？</summary>

```cmd
cd mochibot
.venv\Scripts\activate
git pull
pip install -r requirements.txt
```

然后重新双击 `setup.bat`。
</details>

> **数据不会丢**：`.env`、`data/`（数据库、聊天记录）、`data/prompts/`（自定义 prompt）、`.venv/` 都在 `.gitignore` 里，更新不会碰它们。数据库结构变更会在启动时自动完成。

> 更新后建议对比 `.env.example` 看看有没有新增的配置项——如果有，把新项补进你的 `.env`。（`update.bat` 检测到 `.env.example` 变更时会自动提醒。）

> 如果 `git pull` 报冲突（你手动改过代码文件），先 `git stash` 暂存改动，再 `git pull`，之后 `git stash pop` 尝试恢复。详见 [新手上路手册 > 更新](docs/getting-started.md#6-更新-mochibot)。

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

管理后台默认监听 `localhost`。配置了消息平台（Telegram / WeChat）后会自动绑定到 `0.0.0.0` 并生成 `ADMIN_TOKEN`，但你仍需通过以下方式之一从外部访问：

#### 方式一：SSH 隧道（推荐，安全且无需开放端口）

在你**本地电脑**的终端运行：

```bash
ssh -L 8080:localhost:8080 user@your-server-ip
```

然后在本地浏览器打开 `http://localhost:8080`。流量通过 SSH 加密传输。

#### 方式二：反向代理 + HTTPS（长期使用）

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

MochiBot 有两个配置入口：**`.env` 文件**和**管理后台（Admin Portal）**。

| | `.env` 文件 | 管理后台（Web UI） |
|---|---|---|
| **用途** | 首次启动初始化 + 高级覆盖 | 日常管理 |
| **管理的配置** | Transport token、Admin Portal 设置、日志级别等基础设施 | 模型/API key、心跳参数、时区、Skill 开关、人设 prompt 等运行时配置 |
| **什么时候用** | 部署时填一次，之后一般不碰 | 随时在浏览器里改 |

**它们如何协作：**

1. **首次启动**：`.env` 的值导入数据库，作为初始配置
2. **日常使用**：在管理后台改配置 → 保存到数据库，**同时自动同步回 `.env`**（不会不一致）
3. **高级覆盖**：手动编辑 `.env` + 重启 → `.env` 的值会覆盖数据库（给高级用户留的后门）

> **简单记**：部署时填 `.env`，之后在管理后台改。不要两边同时手动改——以最后操作的为准。

### 核心变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MAIN_PROVIDER` | `openai` | 适配器：`openai` 或 `anthropic` |
| `MAIN_API_KEY` | — | Main 模型的 API key |
| `MAIN_MODEL` | — | Main 模型（必填） |
| `MAIN_BASE_URL` | — | DeepSeek、Gemini 或其他 OpenAI-compatible API 根地址 |
| `TELEGRAM_BOT_TOKEN` | — | 从 @BotFather 获取（Telegram 平台） |
| `WEIXIN_ENABLED` | `false` | 启用 WeChat 平台（与 Telegram 二选一） |
| `HEARTBEAT_INTERVAL_MINUTES` | `20` | 心跳循环间隔 † |
| `ATTENTION_INTERVAL_MINUTES` | `60` | Attention 定期检查间隔 † |
| `FREE_TIME_MIN_MINUTES` / `FREE_TIME_MAX_MINUTES` | `90` / `240` | Free Time 随机间隔范围 † |
| `WAKE_EARLIEST_HOUR` / `SLEEP_AFTER_HOUR` | `6` / `23` | 起床与夜间睡眠窗口 |
| `MAX_DAILY_PROACTIVE` | `5` | 每日主动消息上限 † |
| `TIMEZONE_OFFSET_HOURS` | `8` | 你的 UTC 偏移 † |

> † 首次启动导入数据库，之后在管理后台修改。手动编辑 `.env` + 重启也可覆盖。

<details>
<summary>进阶：Main / Lite、Pre-Router、向量嵌入</summary>

**Main / Lite** — 在管理后台把注册模型分配给两个角色。Main 负责所有有
人格和用户可见的运行；Lite 负责无人格的分类、提取和校验。

**Pre-Router** — `TOOL_ROUTER_ENABLED=true` 启用基于 LLM 的 skill 自动选择。`TOOL_ESCALATION_ENABLED=true`（默认）允许对话中途请求缺少的 skill。

**向量嵌入** — 默认关闭；FTS/LIKE 全文召回不依赖它。OpenAI、阿里云百炼和 Azure AI Foundry 都通过同一个 OpenAI
兼容适配器配置：`EMBEDDING_PROVIDER=openai`、`EMBEDDING_API_KEY`、
`EMBEDDING_MODEL` 和可选的 `EMBEDDING_BASE_URL`。

完整列表见 [.env.example](.env.example)（关键参数）；详见 `mochi/config.py`（~80 个可调参数）。

</details>

Lite 在管理后台显式分配；它可以和 Main 指向同一个注册模型。

---

## 自定义

| 我想改… | 编辑 |
|---------|------|
| 性格、语气、名字、用户与关系上下文 | 管理后台的 Core（`data/core.md`） |
| 记住哪些内容 | `prompts/memory_extract.md` |
| Free Time / Attention 的处境提示 | `prompts/free_time_entry.md` / `prompts/attention_entry.md` |
| 添加 skill 或 observer | 详见 [CONTRIBUTING.md](CONTRIBUTING.md) |

> 性格文件影响最大——改了之后，bot 说话方式就变。

---

## 路线图

- [x] 内置模型预设与 OpenAI-compatible 协议入口
- [x] Main + Lite 模型角色与 Pre-Router
- [x] 持久记忆（连续摘要与提取 + Nightly 归档 / Weekly Main 整理）
- [x] 知识图谱（来源记忆项增量投影 + 对话注入）
- [x] 习惯追踪（频率/重要度/上下文/暂停/延后 + 心跳催促）
- [x] 精确提醒（到点触发 + 循环提醒）
- [x] 饮食记录（自然语言 → 热量估算 + 历史查询）
- [x] 联网搜索（DuckDuckGo，无需 API key）
- [x] 日记系统（今日状态面板 + 夜间归档）
- [x] 管理后台（Web UI）
- [x] Setup Mode（`.env` 只填 bot token 即可启动，通过管理后台完成其余配置）
- [x] 打字节奏（多气泡 + 打字指示器）
- [x] Free Time + Attention（Main Runtime 驱动）
- [ ] 语音消息
- [ ] 多用户支持

## 贡献

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT — 详见 [LICENSE](LICENSE)
