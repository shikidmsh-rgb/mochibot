# MochiBot 使用指南

## 1. 需要准备什么

**AI 模型 API Key**

MochiBot 由 AI API 驱动，目前支持以下 Provider：

| Provider | 接入方式 |
| --- | --- |
| OpenAI / GPT | OpenAI API |
| Anthropic / Claude | Anthropic API |
| DeepSeek | 官方 OpenAI-compatible 接口 |
| Google Gemini | 官方 OpenAI-compatible 接口 |
| 第三方兼容服务 | HTTPS OpenAI-compatible 接口 |

并非每个 Provider、模型和接口组合都经过实测，工具调用、图片等能力以实际使用为准。费用由对应服务商收取。

**电脑或服务器**

支持 Windows、macOS 和 Linux。推荐使用云服务器，避免本地电脑需要一直开机。不熟悉安装和配置的话，可以让 Codex 等 Code Agent 协助操作。

**聊天平台**

微信或 Telegram，二选一。微信在后台扫码登录；Telegram 需要先通过 @BotFather 的 `/newbot` 创建 Bot，取得 Bot Token。

**网络**

运行环境需要能访问所选模型服务和聊天平台。使用 Claude、GPT 或 Telegram 等服务时，按实际网络情况准备代理，本文不展开。

准备好后开始安装。

## 2. 下载与启动

### 下载

默认安装[最新正式版](https://github.com/shikidmsh-rgb/mochibot/releases/latest)，两种方式任选其一：

- **ZIP**：打开正式版页面，在 **Assets** 中下载 **Source code (zip)**，解压。
- **Git**：安装 [Git](https://git-scm.com/downloads)，按 Release 页的版本标签下载。需要聊天自助更新时使用此方式。以下以 `v1.0.22` 为例，发布新版后替换标签：

```bash
git clone --branch v1.0.22 https://github.com/shikidmsh-rgb/mochibot.git
cd mochibot
```

这两种方式安装正式版，不跟随 main。需要未发布改动时，可另选 [main 开发版](https://github.com/shikidmsh-rgb/mochibot/tree/main)，不作为默认安装版本。

### 启动

1. Windows 双击 `setup.bat`；macOS / Linux 在项目目录运行 `bash setup.sh`。
2. 脚本自动创建运行环境并安装依赖。若提示缺少 Python 或版本过低，安装 [Python 3.11+](https://www.python.org/downloads/) 后重新运行；Windows 安装时勾选 **Add Python to PATH**。脚本不安装 Python 本身。
3. 在浏览器打开 `http://127.0.0.1:8080`。

服务器运行与后台访问见第 7 节。

## 3. 首次配置

### 模型

1. 进入「模型 → 添加模型」，填写 Provider、模型名称、API Key 和 API 地址。
2. 保存并点击「测试」。
3. 在「Agent 模型」中分别为 **Main** 和 **Lite** 分配模型。两者都必须配置，可以使用同一个模型。

Main 负责对话、判断和行动，以及 Core、记忆与关系的整理；Lite 负责分类、摘要和记忆提取。

### 消息平台

进入「设置 → 消息平台」：

- **微信**：点击「扫码登录」，扫描二维码并确认。
- **Telegram**：填写 Bot Token，点击「保存并连接」。

同一实例只能启用一个平台，切换会断开原平台。

### 偏好与启动

按「设置」页面完成偏好配置并启动 Mochi，由你发送第一条普通文字消息完成主人绑定。

首次绑定由第一位符合条件的发信人触发。若别人先发送，可能绑定到对方。

### Core（重要）

core是最重要的agent核心！！！虽然他自己也会写但是靠谱度不确定，所以建议常常维护常看常新，必要的内容直接自己更新进去，是和机体验最重要的一个环节~

在「记忆」页面查看和编辑 Core。

## 4. 日常使用

直接聊天即可使用习惯、待办、提醒、日记、搜索和历史查询等能力，例如「明天九点提醒我带材料」「查一下上周的旅行安排」。

### 图片与文件

| 内容 | 支持范围 |
| --- | --- |
| 图片 | Telegram、微信均支持单张、5 MB 以内的图片，需要 Main 支持看图；微信支持 JPG、PNG、GIF、WebP。 |
| 文件 | 微信支持 20 MB 以内的文本、代码、CSV、PDF、Word `.docx`，最多提取前 20,000 字；不支持扫描件 OCR。 |
| 表情包 | Telegram Sticker。 |
| 语音 | 暂未接入模型。 |

原图和文件正文只用于当轮对话，不作为附件保存到聊天历史。不支持的文件格式仅提供文件名等信息。

### 可选配置

| 功能 | 配置 |
| --- | --- |
| 搜索 | 不配置 Key 时使用 Bing；支持 Tavily、百度千帆，Tavily 优先。可通过聊天调整技能配置。 |
| 语义检索 | 在「模型」页配置 Embedding；不配置时仍可文字检索。 |
| 图片生成 | 在「模型 → 图片生成」配置独立服务。目前仅支持微信普通聊天中生成并发送，不支持 Telegram 或主动消息。 |
| 个人扩展 | 默认允许 Mochi 保存资料、维护小指南和开发工具；开发开关在「设置 → 高级」。 |

个人 Python 工具在本机执行，不是沙箱。关闭开发不影响已有资料和已安装工具。详见[个人扩展说明](https://github.com/shikidmsh-rgb/mochibot/blob/main/docs/extensions.md)。

### 自主活动

- **Free Time**：在清醒后的剩余可用时段随机安排，默认时段为当地 06:00–21:00。机会不保证全部触发，也不是必须发满的消息配额；聊天、休息或错过时机时不补发旧消息。
- **Dream**：每天检查积累的材料，达到条件后由 Main 回看日记，整理记忆、Core 和关系，不是每天必做一次总结。
- **提醒**：固定通知到点发送原文；Mochi 的自我提醒则到点后重新判断是否行动或开口。

### 常用命令

两个平台均支持，除 `/help` 外仅对主人生效。

| 命令 | 用途 |
| --- | --- |
| `/help` | 命令帮助 |
| `/heartbeat` | 主动陪伴状态 |
| `/cost` | 今日、本月 Token 用量和模型明细，非服务商账单 |
| `/core`、`/diary` | 查看 Core、今日日记 |
| `/skilloff`、`/skillon` | 轻量闲聊模式、完整模式 |
| `/reset` | 重置短期对话上下文，保留数据库和长期记忆 |
| `/restart` | 重启 |

## 5. 启停与备份

**启动**：运行 `setup.bat` 或 `bash setup.sh`，复用现有数据和配置。

**关闭**：「关闭 Agent」保留管理后台；在运行终端按 `Ctrl+C` 完整退出。浏览器页面关闭不影响运行，进程退出、电脑休眠或断网会影响服务。

**备份**：完整退出后，复制项目目录中的 `data` 文件夹和 `.env` 文件。前者包含聊天、记忆、日记、扩展和数据库配置，后者包含平台凭据及基础配置。

`.env` 中原有的 `ADMIN_TOKEN` 必须随备份保留，它同时用于解密已加密保存的凭据。未配置时，模型 Key 不保证加密存储。

聊天、图片和文件文字会发送给所选模型服务，搜索词会发送给搜索服务，不经过 MochiBot 官方服务器。「运行记录」保留 7 天的详细排障信息，可能包含私人对话；配置、备份和诊断资料均需按私人数据处理。

## 6. 更新

### Git 安装的正式版

通过本指南的启动脚本运行时，可在聊天中要求「检查更新」或「更新到最新正式版」。Mochi 按请求检查正式 Release，先送达回复，再退出安装；保留 `.env`、`data` 和个人扩展。

手动更新步骤：

1. 完整退出，备份 `data` 和 `.env`，确认没有未提交的源码改动。
2. 打开[最新正式版](https://github.com/shikidmsh-rgb/mochibot/releases/latest)，确认目标版本标签，在项目目录执行以下命令。将示例中的 `v1.0.22` 替换为目标标签：

```bash
git fetch origin tag v1.0.22
git checkout --detach v1.0.22
```

3. Windows 运行 `setup.bat`，macOS / Linux 运行 `bash setup.sh`，安装依赖并启动。

`git pull` 和现有 `update.bat` 用于跟踪分支的源码安装，不适用于上述正式版标签安装。有本地源码改动时先处理，不强制覆盖。

### ZIP 安装

1. 完整退出，备份 `data` 和 `.env`。
2. 从最新正式版页面下载 **Source code (zip)**，解压到另一个目录，把原 `data` 和 `.env` 复制进去。
3. 运行新目录中的安装脚本，确认配置和数据正常。新旧实例不能同时运行。

ZIP 安装不支持聊天自助更新或 `update.bat`，补装 Git 不会改变这一点；可另建 Git 安装目录，再恢复原数据和配置。

## 7. 服务器部署

长期运行由进程管理器托管 `scripts/start.py`，以支持重启和聊天自助更新，不直接运行 `python -m mochi.main`。systemd 对应配置示例：

```ini
[Service]
WorkingDirectory=/path/to/mochibot
ExecStart=/path/to/mochibot/.venv/bin/python /path/to/mochibot/scripts/start.py
```

管理后台默认监听服务器的 `127.0.0.1:8080`。在本机建立 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 user@your-server
```

连接后在本机浏览器打开 `http://127.0.0.1:8080`。

使用反向代理时，配置 HTTPS 并在代理层启用访问认证。后台信任回环连接，仅设置 `ADMIN_TOKEN` 不能保护通过本机反向代理转发的访问。

**Docker**：仓库中的 Compose 文件仅为基础示例，后台默认只监听容器内回环地址，后台写入的 `.env` 配置也未持久化到宿主机，尚不是完整的部署配置。Docker 不支持聊天自助更新。

## 常见问题

| 问题 | 检查项 |
| --- | --- |
| 提示 `Python not found` | 安装 Python 3.11+；Windows 勾选 Add Python to PATH，重新运行脚本。 |
| 后台打不开 | 查看启动日志；默认地址为 `http://127.0.0.1:8080`。端口占用时，在 `.env` 修改 `ADMIN_PORT` 并重启。服务器通过 SSH 隧道访问。 |
| 没有启动按钮 | Main、Lite 是否均已分配，平台是否已配置，偏好是否已保存。 |
| 不回复 | 检查运行状态、模型连接和额度、平台连接，以及「系统」「运行记录」中的错误。微信会话过期需重新扫码。 |
| 没有主动消息 | 用 `/heartbeat` 查看状态；检查时区、自由活动开关和清醒状态。长期未聊天会暂停主动活动，机会触发也不保证发消息。 |
| 离线期间错过提醒 | 过期主动消息不补发；定时提醒超过五分钟有效期会过期。 |

问题反馈：[GitHub Issues](https://github.com/shikidmsh-rgb/mochibot/issues)。提交日志前移除凭据和私人内容。
