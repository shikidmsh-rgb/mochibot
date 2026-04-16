# MochiBot 贡献指南

MochiBot 目前处于早期 alpha 阶段，欢迎贡献！

1. Fork 仓库
2. 创建 feature 分支
3. 遵循 [ARCHITECTURE.md](ARCHITECTURE.md) 中的架构规则
4. 提交 PR

---

## 添加自定义 Skill

完整规范：**[docs/SKILL_SPEC.md](docs/SKILL_SPEC.md)** | 模板：**[docs/skill_template/](docs/skill_template/)**

### 快速开始

```bash
cp -r docs/skill_template/ mochi/skills/my_skill/
# 编辑 SKILL.md + handler.py，重启 bot
```

### 目录结构

```
mochi/skills/my_skill/
├── __init__.py          # 空文件（必须）
├── SKILL.md             # 元数据 + 工具定义（启动时解析）
├── handler.py           # Skill 子类，实现 execute()
├── queries.py           # （可选）DB 查询 — 仅在需要持久化时添加
└── observer.py          # （可选）周期性数据采集
```

### SKILL.md

```yaml
---
name: my_skill
description: "技能功能描述 — pre-router 靠这个分类，务必写清楚"
type: tool
expose_as_tool: true
---
```

工具定义格式：`## Tools` → `### tool_name (风险等级)` → 参数表：

```markdown
## Tools

### my_tool (L1)
工具功能描述。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string | yes | add / list / delete |
| item | string | | Item content (action=add) |
```

风险等级：`L0` 只读、`L1` 内部状态写入（默认）、`L2` 外部写入（暂保留）、`L3` 事务性（暂保留）。

### handler.py

```python
from mochi.skills.base import Skill, SkillContext, SkillResult

class MySkill(Skill):

    def init_schema(self, conn) -> None:
        """Create DB tables (called at startup). Only if skill needs storage."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS my_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

    async def execute(self, context: SkillContext) -> SkillResult:
        action = context.args.get("action")
        if action == "add":
            from mochi.skills.my_skill.queries import create_item
            item_id = create_item(context.user_id, context.args["item"])
            return SkillResult(output=f"Added #{item_id}.")
        return SkillResult(output=f"Unknown action: {action}", success=False)
```

### 开发规则

1. **禁止修改 `db.py`** — 表定义放 `init_schema()`，查询放 `queries.py`
2. **禁止导入其他 skill** — skill 之间完全隔离，不能互相调用
3. **禁止向上导入** — `skill → heartbeat`、`skill → ai_client`、`skill → transport` 都不允许。只能 import `mochi.skills.base`、`mochi.db`、`mochi.config`、`mochi.llm`、标准库
4. **只用 `CREATE TABLE IF NOT EXISTS`** — `init_schema()` 中禁止破坏性 DDL
5. **禁止跨 skill 外键** — 每个 skill 只拥有自己的表
6. **用 `ensure_column()` 做 schema 迁移**（从 `mochi.db` 导入）
7. **平台不兼容时声明 `exclude_transports`** — 如果 skill 依赖特定平台特性（如 Telegram file_id），在 SKILL.md 中用 `exclude_transports: [wechat]` 排除不兼容平台。不确定时不要排除
8. **代码全英文** — 变量名、函数名、注释、docstring 一律英文

重启 MochiBot → 日志中看到 `Registered skill: my_skill` 即成功。

禁用方式：重命名 `SKILL.md` → `SKILL.md.disabled`，或在管理面板中切换。

---

## 添加自定义 Observer

Observer 是被动的只读传感器，为 Heartbeat Think 步骤提供上下文 — 零 LLM 调用。

两种类型：

| 类型 | 位置 | 开关 | 示例 |
|------|------|------|------|
| **共置型** | `mochi/skills/{name}/` | 跟随 skill 开关 | oura, weather |
| **基础设施型** | `mochi/observers/{name}/` | 始终运行 | time_context, activity_pattern |

### 共置型 Observer（推荐用于 skill 相关数据）

如果你的 skill 需要周期性采集数据给 heartbeat：

1. 在 SKILL.md 中添加 `sense:` 块：

```yaml
---
name: my_skill
expose_as_tool: true
sense:
  interval: 30
---
```

2. 在同一 skill 目录下创建 `observer.py` + `OBSERVATION.md`：

```
mochi/skills/my_skill/
├── __init__.py
├── SKILL.md             # 包含 sense: 块
├── handler.py           # skill 逻辑
├── observer.py          # observer 逻辑
└── OBSERVATION.md       # observer 元数据
```

3. OBSERVATION.md：

```markdown
---
name: my_skill
interval: 30
type: source
enabled: true
requires_config: [MY_API_KEY]
skill_name: my_skill
---

这个 observer 监测什么。
```

4. observer.py：

```python
from mochi.observers.base import Observer

class MyObserver(Observer):
    async def observe(self) -> dict:
        # Your logic here — return {} to report nothing
        return {"value": 42}
```

Skill 在管理面板中被禁用时，其共置 observer 也会自动跳过。

### 基础设施型 Observer（跨技能的通用上下文）

不绑定特定 skill 的 observer：

```
mochi/observers/my_observer/
├── __init__.py
├── OBSERVATION.md
└── observer.py
```

### Observer 类型

| 类型 | 含义 |
|------|------|
| `source` | 从外部 API 或服务获取数据 |
| `context` | 从内部状态（DB、运行时）派生上下文 |

### 关键行为

- 启动时 `requires_config` 中任一环境变量缺失 → **自动禁用**
- **间隔节流**：`safe_observe()` 在距上次运行不足 `interval` 分钟时跳过
- **错误隔离**：异常被捕获并记录，失败时返回 `{}`
- **连续失败 5 次** → 本次会话自动禁用
- **Delta 检测**：重写 `has_delta(prev, curr)` 可抑制无变化时的 Think 触发
- 返回 `{}` 表示静默跳过（如 API 不可用）

重启 MochiBot → 日志中看到 `Registered observer: my_observer` 即成功。

禁用方式：在 `OBSERVATION.md` 中设 `enabled: false`，或重命名为 `OBSERVATION.md.disabled`。

---

## 开发 & 测试（不需要 API key 或 bot token）

MochiBot 的测试基建完全自包含——**不需要 `.env`、不需要 API key、不需要 Telegram bot token**。Code agent 或开发者 clone 后直接就能写代码和跑测试。

### 环境准备

```bash
git clone https://github.com/shikidmsh-rgb/mochibot.git && cd mochibot
python3 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install pytest pytest-asyncio                    # 测试依赖
```

不需要创建 `.env` 文件。所有测试通过 fixture 自动 mock 配置。

### 跑测试

```bash
pytest                  # 全部测试
pytest tests/           # 仅单元测试
pytest tests/e2e/       # 自包含 E2E + 自动跳过的 live E2E
```

`tests/` 和默认的 `tests/e2e/` 运行不需要 `.env`。只有显式设置 `MOCHIBOT_RUN_LIVE_E2E=1` 时，live E2E 才会执行。

```powershell
$env:MOCHIBOT_RUN_LIVE_E2E = "1"
pytest tests/e2e/test_prerouter_live.py -v -s
pytest tests/e2e/test_chat_proactive_e2e.py -v -s
```

live E2E 需要在一个已经配置好 `.env` 和 `data/mochi.db` 的本地 checkout 中运行；缺任一条件时会自动 skip。

### 测试基建

测试分两层，基础 fixture 在 `conftest.py` 中 autouse（自动生效），无需手动设置：

| 层级 | 位置 | 自动生效（autouse） | 按需使用 |
|------|------|-------------------|---------|
| **单元测试** | `tests/` | `fresh_db`（独立 DB）、`mock_config`（覆盖 config） | — |
| **E2E 测试** | `tests/e2e/` | 以上 + `discover_skills`、`reset_tool_policy`、`reset_heartbeat_state` | `mock_llm_factory`（脚本化 LLM，需声明为测试参数）、`FakeTransport`（需手动 import） |

说明：`tests/e2e/test_prerouter_live.py` 和 `tests/e2e/test_chat_proactive_e2e.py` 属于 live E2E。它们默认自动 skip，只有在显式设置 `MOCHIBOT_RUN_LIVE_E2E=1` 且本地 checkout 已配置真实 `.env`/`data/mochi.db` 时才会运行。

### 给新 Skill 写测试

在 `tests/` 下创建 `test_my_skill_handler.py`（单元测试），或在 `tests/e2e/` 下创建 E2E 测试。

**E2E 测试示例**——验证完整的 消息 → LLM → 工具调用 → DB 流程：

```python
import pytest
from mochi.transport import IncomingMessage
from mochi.ai_client import chat
from tests.e2e.mock_llm import make_response, make_tool_call

def _msg(text, user_id=1):
    return IncomingMessage(user_id=user_id, channel_id=100,
                           text=text, transport="fake")

class TestMySkill:
    @pytest.mark.asyncio
    async def test_add_item(self, mock_llm_factory):
        # 脚本化 LLM：先返回工具调用，再返回最终回复
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("my_tool", {"action": "add", "item": "买菜"}),
            ]),
            make_response("已添加！"),
        ])

        reply = await chat(_msg("帮我记一下买菜"))
        assert "已添加" in reply.text
```

关键点：
- `mock_llm_factory` 注入脚本化 LLM 响应，**按顺序消费**
- `make_tool_call` 模拟 LLM 发起工具调用
- `fresh_db` 保证每个 test 的 DB 是干净的
- 整个流程不需要网络、API key 或运行中的 bot

### 什么能测、什么不能测

| 能测（不需要 API key） | 不能测 |
|----------------------|--------|
| Skill handler 逻辑 | Telegram / WeChat 交互 |
| 完整 chat → tool → DB 流程 | 真实 LLM 响应质量 |
| 记忆提取、召回 | 心跳主动消息发送 |
| DB 读写、schema 迁移 | Observer 外部 API 调用（如 Oura） |
| Admin portal API | |

---

## 代码风格

- **代码**：全英文（变量名、函数名、注释、docstring）
- **Commit messages**：英文，[conventional commits](https://www.conventionalcommits.org/)
- 参见 [ARCHITECTURE.md](ARCHITECTURE.md) 了解分层规则和依赖方向
