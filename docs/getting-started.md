# 新手上路：本地部署 MochiBot

跟着做，大约 10 分钟就能让 bot 跑起来。

---

## 目录

1. [你需要准备什么](#1-你需要准备什么)
2. [下载 MochiBot](#2-下载-mochibot)
3. [运行安装](#3-运行安装)
4. [管理后台配置](#4-管理后台配置)
5. [Skill 开关](#5-skill-开关)
6. [注意事项](#6-注意事项)
7. [更新 MochiBot](#7-更新-mochibot)
8. [常见问题](#8-常见问题)

---

## 1. 你需要准备什么

### Python

MochiBot 需要 Python 3.11 或更高版本。没装的话去 [python.org/downloads](https://www.python.org/downloads/) 下载。

**Windows 用户安装时记得勾选 "Add Python to PATH"**——这个很重要，不勾的话后面会报错。

### 一个 LLM API Key

MochiBot 本身不包含 AI 模型，需要你提供一个 API key（调用 AI 的"通行证"）。去你选的 AI 平台注册获取即可，比如 OpenAI、DeepSeek、Anthropic 等。

> 这些服务按用量收费，日常使用一个月大概几块到几十块人民币。

### 消息平台（二选一）

MochiBot 需要一个消息平台来跟你聊天。**Telegram 或微信，选一个就行。**

**选 Telegram？** 你需要提前创建一个 bot token：

1. 在 Telegram 搜索 **@BotFather**，点进去发消息
2. 发送 `/newbot`
3. 给 bot 起个名字（显示名），比如 `My Mochi`
4. 再起个用户名（必须以 `bot` 结尾），比如 `my_mochi_bot`
5. BotFather 会回复一串 token，长得像 `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`
6. **复制保存好这个 token**，等下要用

**选微信？** 不需要提前准备任何东西。后续在管理后台配置时会引导你用个人微信号扫码登录。

> Telegram 支持表情包、语音等更丰富的交互，推荐优先考虑。

---

## 2. 下载 MochiBot

1. 打开 MochiBot 的 GitHub 页面：[github.com/shikidmsh-rgb/mochibot](https://github.com/shikidmsh-rgb/mochibot)
2. 点击页面上**绿色的 "Code" 按钮**
3. 在弹出的菜单里点 **"Download ZIP"**
4. 下载完成后，**解压 ZIP 文件**到你想放的位置（比如桌面或文档文件夹）

解压后你会得到一个 `mochibot-main` 文件夹，里面就是全部代码。

---

## 3. 运行安装

打开刚才解压的文件夹，找到 `setup.bat`（Windows）或 `setup.sh`（macOS/Linux），**双击运行**。

> macOS/Linux 如果双击没反应，右键文件 → 在终端中打开，然后输入 `bash setup.sh` 回车。

脚本会自动：
- 创建独立的 Python 环境（不会影响你电脑上的其他程序）
- 安装所有依赖
- 打开管理后台

看到类似这样的输出就说明成功了：

```
  [OK] Python 3.xx
  [OK] Dependencies installed.

  Setup complete!
  Opening admin portal at http://127.0.0.1:8080
```

浏览器会自动打开管理后台页面。如果没自动打开，手动在浏览器地址栏输入 `http://127.0.0.1:8080`。

---

## 4. 管理后台配置

管理后台打开后，你会看到一个设置引导页面（「设置向导」），**照着页面上的提示走就行**。

这里补充几个页面上没有详细说的点：

### 谁是 Owner？

配置好启动 bot 之后，**第一个在 Telegram 上给你 bot 发消息的人**就会自动成为 owner（主人）。Owner 可以使用所有功能和管理指令。

### 人设很重要

设置向导会让你编辑一个叫 `soul.md` 的文件——这个文件决定了 bot 的性格、语气、关注点。

你可以定义 TA 是温柔的、毒舌的、打工人风格的……随你。改了这个文件之后，bot 说话方式会完全不同。

不知道写什么？先用默认的就行，之后随时可以在管理后台修改。

---

## 5. Skill 开关

### 什么是 Skill？

Skill 就是 bot 的"能力模块"。每个 Skill 负责一件事，可以独立开关。

### 默认的 Skill

| Skill | 干什么 | 需要额外配置吗？ |
|-------|--------|-----------------|
| **habit**（习惯） | 追踪日常习惯，打卡、催你 | 不需要 |
| **todo**（待办） | 记录待办事项 | 不需要 |
| **reminder**（提醒） | 定时提醒，支持重复 | 不需要 |
| **meal**（饮食） | 记录饮食、估算热量 | 不需要 |
| **memory**（记忆） | 记住你说过的事 | 不需要 |
| **note**（笔记） | 随手记笔记 | 不需要 |
| **web_search**（搜索） | 联网搜索信息 | 不需要 |
| **weather**（天气） | 查天气 | 不需要（想让心跳带天气信息需设城市名） |
| **sticker**（表情包） | 学你发的表情包，聊天时自己发 | 不需要（仅 Telegram） |
| **oura** | 接入 Oura Ring 健康数据 | **需要** Oura API 凭据 |

### 怎么开关？

在管理后台的 **「Skills」** 页签里，每个 Skill 旁边有开关按钮，点一下就行。

> 不确定要不要开？**全开着就好**。用不上的 Skill 不会打扰你——bot 只有在你聊到相关话题时才会调用对应的 Skill。

---

## 6. 注意事项

**电脑关了 = bot 离线**
MochiBot 运行在你的电脑上。电脑关机、合盖、断网，bot 就不会回消息。想让 bot 24 小时在线，需要部署到服务器上——见 [README 的部署章节](../README.md#部署)。

**数据都在 `data/` 文件夹里**
聊天记录、记忆、习惯数据都在这。想备份？复制这个文件夹就行。

**保管好你的配置**
`.env` 文件里有你的 API key 和 bot token。不要分享给别人。

**日常启动**
每次想用 bot，双击 `setup.bat`（Windows）或运行 `setup.sh`（macOS/Linux）就行。关闭 bot：直接关掉窗口。

---

## 7. 更新 MochiBot

MochiBot 会不定期发布新版本。你的数据（聊天记录、配置、API key）不会被更新覆盖。

**更新方法：** 先关掉正在运行的 bot，然后双击 `update.bat`（在 mochibot 文件夹里，和 setup.bat 挨着）。脚本会自动拉取最新代码、安装新依赖、启动 bot。

> 如果你是通过 Download ZIP 安装的（没有用 Git），update.bat 会提示你需要先安装 Git。按提示去 [git-scm.com](https://git-scm.com/downloads/win) 下载安装即可，之后 update.bat 就能正常工作了。

---

## 8. 常见问题

### 运行脚本报错 "Python not found"

Python 没装，或者没加到系统路径里。重新安装 Python，**一定勾选 "Add Python to PATH"**。

### 管理后台打不开

1. 确认脚本窗口还在运行、没有报错
2. 浏览器里输入 `http://127.0.0.1:8080`（不是 https）
3. 端口被占了？在 `.env` 里加一行 `ADMIN_PORT=9090` 换个端口

### Bot 不回消息

按顺序检查：
1. 脚本窗口里有没有红色错误信息？
2. 去管理后台确认 API key 没有多余的空格
3. 确认模型名写对了（比如是 `gpt-4o` 不是 `gpt4o`）
4. API 余额够吗？去对应平台看看

### Bot 不主动找我

这是正常的。MochiBot 的心跳机制每 20 分钟检查一次，觉得有必要才会找你。刚启动的话等一会儿就好。另外 bot 默认晚上 11 点到早上 7 点不会主动打扰你。

---

> 还有问题？去 [GitHub Issues](https://github.com/shikidmsh-rgb/mochibot/issues) 提问。
