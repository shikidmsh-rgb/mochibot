# 新手上路：本地部署 MochiBot

这篇手册假设你从来没用过命令行。跟着做，大约 10 分钟就能让 bot 跑起来。

---

## 目录

1. [你需要准备什么](#1-你需要准备什么)
2. [开始](#2-开始)
3. [管理后台配置](#3-管理后台配置)
4. [Skill 开关](#4-skill-开关)
5. [注意事项](#5-注意事项)
6. [更新 MochiBot](#6-更新-mochibot)
7. [常见问题](#7-常见问题)

---

## 1. 你需要准备什么

开始之前，确认你有这些：

- **一台联网的电脑**（Windows、macOS、Linux 都行）
- **Python 3.11+** — 没装的话去 [python.org/downloads](https://www.python.org/downloads/) 下载。**Windows 用户安装时记得勾选 "Add Python to PATH"**
- **Git** — 没装的话：Windows 去 [git-scm.com](https://git-scm.com/downloads/win) 下载；macOS 打开终端输入 `git` 会自动提示安装；Linux 用包管理器装（`sudo apt install git`）

> 后面的安装脚本会自动检查 Python 版本、创建虚拟环境、安装所有依赖——你只需要确保 Python 和 Git 装好了就行。

### 一个 LLM API Key

API key 就是你调用 AI 模型的"通行证"。MochiBot 本身不包含 AI 模型，需要你提供一个。

推荐的提供商（选一个就行）：

| 提供商 | 特点 | 获取 API Key |
|--------|------|-------------|
| **OpenAI** | GPT-4o，最主流 | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| **DeepSeek** | 便宜好用，中文优秀 | [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) |
| **Anthropic** | Claude 系列 | [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) |

> 这些服务按用量收费。日常使用一个月大概几块到几十块人民币，取决于你聊多少。

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

## 2. 开始

### 第一步：打开终端

- **Windows**：按 `Win` 键，搜索 `cmd` 或 `终端`，点击打开
- **macOS**：按 `Cmd + 空格`，搜索 `Terminal`，回车打开
- **Linux**：按 `Ctrl + Alt + T`

你会看到一个黑色（或白色）的窗口，里面有个光标在闪——这就是终端。后面的命令都在这里输入。

### 第二步：下载 MochiBot

在终端里输入（按回车执行）：

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git
```

等它下载完，再输入：

```bash
cd mochibot
```

### 第三步：运行安装脚本

**Windows：**

```bash
setup.bat
```

或者直接在文件管理器里双击 `setup.bat`。

**macOS / Linux：**

```bash
bash setup.sh
```

脚本会自动：
- 创建独立的 Python 环境（不会影响你电脑上的其他 Python 程序）
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

## 3. 管理后台配置

管理后台打开后，你会看到一个设置引导页面（「设置向导」），它会一步步带你完成配置——**照着页面上的提示走就行**。

这里补充几个页面上没有详细说的点：

### 谁是 Owner？

配置好启动 bot 之后，**第一个在 Telegram 上给你 bot 发消息的人**就会自动成为 owner（主人）。Owner 可以使用所有功能和管理指令。

### 人设很重要

设置向导会让你编辑一个叫 `soul.md` 的文件——这个文件决定了 bot 的性格、语气、关注点。

你可以定义 TA 是温柔的、毒舌的、打工人风格的……随你。改了这个文件之后，bot 说话方式会完全不同。

不知道写什么？先用默认的就行，之后随时可以在管理后台修改。

---

## 4. Skill 开关

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

## 5. 注意事项

**电脑关了 = bot 离线**
MochiBot 运行在你的电脑上。电脑关机、合盖、断网，bot 就不会回消息，也不会主动找你。想让 bot 24 小时在线，需要部署到服务器上——见 [README 的部署章节](../README.md#部署)。

**数据在 `data/` 文件夹里**
所有聊天记录、记忆、习惯数据都存在项目的 `data/` 目录下。想备份？复制这个文件夹就行。

**保管好你的数据**
`.env` 文件里有你的 API key 和 bot token，`data/` 目录的数据库里也存了一份。不要把这两样分享给别人，也不要提交到 Git。

**启动方式**
| 场景 | 命令 |
|------|------|
| 第一次设置（还没配好） | 运行 `setup.bat` / `bash setup.sh`（打开管理后台） |
| 已经配好，日常启动 bot | 运行 `python scripts/start.py`（启动 bot 本体） |

`scripts/start.py` 支持自动重启——如果你在管理后台点了"重启"按钮，bot 会自动重启，不用你手动操作。

---

## 6. 更新 MochiBot

MochiBot 会不定期发布新版本（修 bug、加功能）。更新很简单，你的数据不会丢。

### 你的数据是安全的

以下内容 **不会** 被更新覆盖——它们都不在 Git 管理范围内：

| 内容 | 位置 | 说明 |
|------|------|------|
| 数据库（聊天记录、记忆、习惯等） | `data/` | 所有用户数据 |
| 自定义 prompt | `data/prompts/` | 你在管理后台改过的人设等 |
| 配置文件 | `.env` | 你的 API key、token 等 |
| Python 环境 | `.venv/` | 依赖包 |

所以放心更新，不会影响你的任何数据和配置。

### 更新步骤

#### Windows 用户：双击 `update.bat`

1. **先关掉 MochiBot**——把 `setup.bat` 的黑窗口关掉
2. **双击 `update.bat`**（在 mochibot 文件夹里，和 setup.bat 挨着）
3. 等脚本跑完，会自动开新窗口启动 bot

脚本会自动完成：拉最新代码 → 装新依赖 → 启动 bot。
如果中途出错，会**清晰报错并停下**，按提示操作即可。

> **如果 `update.bat` 报"端口 8080 被占用"**——bot 还在跑。回到 `setup.bat` 的窗口，按 Ctrl+C 或直接关掉，再重新双击 update.bat。

#### macOS / Linux 用户：命令行更新

打开终端，进入 mochibot 文件夹：

```bash
cd mochibot
source .venv/bin/activate
git pull
pip install -r requirements.txt
```

然后重新跑 `bash setup.sh` 启动 bot。

#### 检查新配置项（更新脚本会自动提醒）

新版本可能会新增配置项。`update.bat` 检测到 `.env.example` 有变更时会自动提示你。

如果你是手动更新（macOS / Linux 或不用脚本），可以这样对比：

```bash
git diff HEAD@{1} HEAD -- .env.example
```

如果有新增的变量，把需要的补进你的 `.env` 文件就行。不确定要不要加？大部分新配置都有默认值，不加也能正常运行。

> 数据库结构变更会在 bot 启动时自动完成，不需要你手动操作。

### 遇到冲突怎么办？

如果你手动改过 `mochibot` 文件夹里的代码文件（不是 `.env` 或 `data/`），`git pull`（包括 `update.bat`）可能会报错，类似：

```
error: Your local changes to the following files would be overwritten by merge
```

**解决方法**——把你的改动暂存起来，更新完再恢复。在 mochibot 文件夹里打开终端（Windows 可以在文件管理器地址栏输入 `cmd` 回车），运行：

```bash
git stash                    # 暂存你的改动
git pull                     # 拉取最新代码
git stash pop                # 恢复你的改动
```

如果 `git stash pop` 也报冲突（你改的地方和新版本改的地方重叠了），Git 会在文件里标记冲突位置。如果你看不懂这些标记，最简单的办法是放弃你的改动，用新版本的代码：

```bash
git checkout .               # 放弃本地改动，用新版本
```

然后再双击 `update.bat`（或 macOS/Linux 重新跑 setup）。

> **提示**：一般不建议直接修改代码文件。需要自定义的内容（人设、配置）都可以通过管理后台或 `.env` 搞定。

---

## 7. 常见问题

### 运行脚本报错 "Python not found"

Python 没装，或者没加到系统路径里。

- Windows：重新安装 Python，**一定勾选"Add Python to PATH"**
- macOS/Linux：试试 `python3 --version`，有些系统命令是 `python3` 不是 `python`

### 管理后台打不开

1. 确认终端里没有报错，脚本正常在运行
2. 浏览器里输入 `http://127.0.0.1:8080`（不是 https）
3. 端口被其他程序占了？在 `.env` 里加一行 `ADMIN_PORT=9090`，换个端口

### Bot 不回消息

按顺序检查：

1. **终端里有报错吗？** 看看有没有红色错误信息
2. **API key 对吗？** 去管理后台确认 key 没有多余的空格
3. **模型名对吗？** 比如是 `gpt-4o` 不是 `gpt4o`
4. **Telegram token 对吗？** 重新从 BotFather 复制一次
5. **API 余额够吗？** 去对应平台看看是不是欠费了

### Bot 不主动找我

这是正常的。MochiBot 的"主动找你"靠**心跳**机制——后台每 20 分钟检查一次你的习惯、待办、提醒，觉得有必要才会找你。

- 刚启动？等一会儿，第一次心跳需要最多 20 分钟
- 检查管理后台的「心跳」状态是不是正常运行中
- bot 也有"作息"——默认晚上 11 点到早上 7 点不会主动打扰你

### 想换模型 / 换 API key

直接在管理后台改就行，改完 bot 会自动使用新的配置。大部分配置改完不需要重启。

### 怎么重置所有数据？

删掉 `data/` 文件夹，然后重启 bot。所有聊天记录、记忆、习惯数据都会清空，回到全新状态。

### 怎么更新 MochiBot？

见上方 [第 6 节：更新 MochiBot](#6-更新-mochibot)。

### 装了但 `pip install` 报错

可能是网络问题。试试：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

这会用清华镜像源下载，国内会快很多。

---

> 还有问题？去 [GitHub Issues](https://github.com/shikidmsh-rgb/mochibot/issues) 提问。
