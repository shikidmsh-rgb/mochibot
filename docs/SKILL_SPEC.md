# MochiBot Skill 开发规范

Skill 是自包含的功能模块。本文档涵盖创建一个 skill 所需的全部内容。

## 快速开始

```bash
# 1. 复制模板
cp -r docs/skill_template/ mochi/skills/my_skill/

# 2. 编辑 SKILL.md（工具定义）和 handler.py（逻辑）

# 3. 重启 MochiBot — 日志中应看到：
#    INFO  Registered skill: my_skill (type=tool, tools=['my_tool'], triggers=['tool_call'])
```

## 目录结构

```
mochi/skills/my_skill/
├── __init__.py        # 空文件（Python 包必须）
├── SKILL.md           # 工具定义、元数据、使用规则
├── handler.py         # Skill 子类，实现 execute() 方法
├── queries.py         # （可选）DB 查询 — 仅在需要持久化时添加
└── observer.py        # （可选）周期性数据采集的 Observer
```

## SKILL.md 参考

SKILL.md 是 skill 元数据和工具定义的**唯一真实来源（SSOT）**。

### Front-Matter 字段

```yaml
---
# 必填
name: my_skill                    # 唯一标识符
description: "技能功能描述 — pre-router 靠这个做分类，务必写清楚"

# 常用可选
type: tool                        # tool（默认）| automation | hybrid
expose_as_tool: true              # true（默认）— 是否注入到 LLM 工具列表
tier: chat                        # lite | chat（默认）| deep — 模型等级
keywords: [keyword1, 关键词2]     # Pre-router 关键词回退（零 LLM 调用）
multi_turn: false                 # true 表示 skill 可能需要多轮工具调用
core: false                       # true = 不可在管理面板中被禁用

# 日记集成
diary_status_order: 50            # 数字越小在 今日状態 面板中越靠上（默认 50）
writes:
  diary: [diary]                  # 此 skill 写入的日记区段
  db: [my_table]                  # 此 skill 写入的 DB 表

# 配置（需要外部凭据/设置的 skill）
requires_config: [MY_API_KEY]     # 必需的环境变量或 DB 配置（简写）
# 也支持嵌套写法（等价）：
# requires:
#   env: [MY_API_KEY]
config:
  MY_API_KEY:
    type: str
    default: ""
    secret: yes
    description: "API key for external service"

# 平台排除
exclude_transports: [wechat]      # 隐藏此 skill 的平台

# 子技能（暴露多组工具的 skill）
sub_skills:
  my_manage: "管理工具 — 删除、编辑"

# Observer（周期性数据采集）
sense:
  interval: 30                    # Observer 运行间隔（分钟）
---
```

### 工具定义格式

在 front-matter 之后，用 `## Tools` 区段定义工具：

```markdown
## Tools

### my_tool (L1)
工具功能描述。

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string | yes | add / list / delete |
| item | string | | Item description (action=add) |
| item_id | integer | | Item ID (action=delete) |
```

**风险等级**（追加在工具名后）：
- `L0` — 只读，无副作用
- `L1` — 内部状态写入（创建/修改数据，默认）
- `L2` — 外部写入（有副作用），暂保留
- `L3` — 事务性操作（支付/订单），暂保留

### Usage Rules 区段

添加 `## Usage Rules` 区段，内容会注入到 LLM system prompt 中，指导 LLM 何时/如何使用此工具：

```markdown
## Usage Rules

- 用户说 "X" 时，用 action=add
- 用户说 "Y" 时，用 action=delete
- 不要用此工具做 Z — 改用 skill_name
```

### keywords 字段

`keywords` 提供零 LLM 调用的回退分类。当 LLM pre-router 失败或返回空时，框架会在用户消息中搜索这些关键词来匹配 skill。

```yaml
keywords: [remind, 提醒, alarm, 闹钟, timer, 定时]
```

注意事项：
- 只放**高精度**关键词 — 能明确指向此 skill 的词
- 中英文都要有
- 支持多词短语（如 `web search`）
- 列表保持精简（5-10 个关键词）
- **不要去改 `tool_router.py`** — 框架会自动从 SKILL.md 读取

### 平台兼容性（`exclude_transports`）

MochiBot 支持多个消息平台（transport）。部分 skill 可能只在特定平台上才有意义，或因平台限制无法正常工作。用 `exclude_transports` 声明不兼容的平台：

```yaml
exclude_transports: [wechat]
```

**当前支持的 transport 值**：`telegram`、`wechat`

**框架行为**：被排除平台上，该 skill 的工具：
- 不出现在 LLM 工具列表中（`get_tools()` 过滤）
- 不出现在能力摘要中（`_build_capability_summary()` 过滤）
- 即使被直接调用也会被拒绝（`dispatch()` 返回 "not available on this platform"）

**实际示例** — `sticker` skill 排除 wechat：
```yaml
# mochi/skills/sticker/SKILL.md
exclude_transports: [wechat]
```
原因：sticker 使用 Telegram 的 `file_id` 机制存储和发送贴纸，这个机制在 WeChat 上不存在。

**注意事项**：
- 大多数 skill 不需要此字段 — 默认在所有平台可用
- 只在技术上确实不兼容时才排除，不要仅因为"没测过"就排除
- 可排除多个平台：`exclude_transports: [wechat, telegram]`

## Handler 类

在 `handler.py` 中创建 `Skill` 子类：

```python
"""My skill handler — execute logic only. Tool defs in SKILL.md."""

from mochi.skills.base import Skill, SkillContext, SkillResult


class MySkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = args.get("action")
        uid = context.user_id

        if action == "add":
            # ... your logic ...
            return SkillResult(output="Added successfully.")

        elif action == "list":
            # ... your logic ...
            return SkillResult(output="Here are your items.")

        return SkillResult(output=f"Unknown action: {action}", success=False)
```

### SkillContext

框架传入 `SkillContext` 到 `execute()`：

| 属性 | 类型 | 说明 |
|------|------|------|
| `trigger` | str | 触发方式（`"tool_call"`、`"cron"` 等） |
| `user_id` | int | 触发 skill 的用户 |
| `channel_id` | int | 聊天频道 ID |
| `transport` | str | 平台标识（`"telegram"`、`"wechat"` 等） |
| `tool_name` | str | 被调用的工具名称 |
| `args` | dict | 传给工具的参数 |
| `observation` | dict \| None | 仅 heartbeat 触发时有值，包含 observer 数据。普通 tool 类 skill 不用关心 |

### SkillResult

`execute()` 返回 `SkillResult`：

| 属性 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `output` | str | `""` | 返回给用户的文本 |
| `actions` | list[dict] | `[]` | heartbeat 风格的动作列表（如 `[{"type": "message", "content": "..."}]`），普通 skill 一般不用 |
| `success` | bool | `True` | 操作是否成功 |

### 可选方法

#### `init_schema(self, conn) -> None`

创建 skill 需要的 DB 表。启动时由框架传入 SQLite 连接调用。

```python
def init_schema(self, conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS my_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            content    TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        );
    """)
    # 用 ensure_column() 做 schema 迁移：
    from mochi.db import ensure_column
    ensure_column(conn, "my_items", "priority", "INTEGER DEFAULT 0")
```

规则：
- 只用 `CREATE TABLE IF NOT EXISTS` — 禁止破坏性 DDL
- 只创建**本 skill** 的表 — 禁止跨 skill 外键
- 框架会在你的方法返回后自动 `conn.commit()`
- 用 `mochi.db` 中的 `ensure_column()` 添加列到已有表

#### `diary_status(self, user_id, today, now) -> list[str] | None`

为每日状态面板贡献行：

```python
def diary_status(self, user_id, today, now):
    items = get_my_items(user_id)
    if not items:
        return None
    return [f"- {item['content']} ({'done' if item['done'] else 'pending'})"
            for item in items]
```

#### `get_tools(self) -> list[dict]`

仅在需要动态工具定义时重写（极少）。默认情况下工具从 SKILL.md 解析。

## DB 查询 (`queries.py`)

如果 skill 需要持久化存储，把所有 DB 函数放在 `queries.py` 中：

```python
"""My skill — DB queries."""

from datetime import datetime
from mochi.db import _connect
from mochi.config import TZ


def create_item(user_id: int, content: str) -> int:
    """Add an item. Returns the new item id."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO my_items (user_id, content, created_at) VALUES (?, ?, ?)",
        (user_id, content, now),
    )
    conn.commit()
    item_id = cur.lastrowid
    conn.close()
    return item_id


def get_items(user_id: int) -> list[dict]:
    """Get all items for a user."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, content, created_at FROM my_items WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

在 handler 中导入：

```python
from mochi.skills.my_skill.queries import create_item, get_items
```

## Observer（可选）

需要周期性数据采集的 skill，添加 `observer.py` + `OBSERVATION.md`。

参考已有 observer（如 `mochi/skills/oura/observer.py`）了解完整模式。Observer 独立于工具调用运行，写入的数据供 skill 或 heartbeat 后续读取。

## 配置系统

Skill 在 SKILL.md 中声明所需配置：

```yaml
requires_config: [MY_API_KEY]
config:
  MY_API_KEY:
    type: str
    default: ""
    secret: yes
    description: "API key for external service"
```

**解析优先链**（从高到低）：
1. 管理面板（DB `skill_config` 表）
2. 环境变量（`.env` 文件）
3. Schema 默认值

缺少必需配置的 skill 会被自动禁用（不出现在工具列表中）。

在 handler 中访问配置：

```python
value = self.get_config("MY_API_KEY")
```

## 测试

在 `tests/test_my_skill_handler.py` 中创建测试：

```python
import pytest
from mochi.skills.base import SkillContext, SkillResult
from mochi.skills.my_skill.handler import MySkill


class TestMySkill:

    @pytest.mark.asyncio
    async def test_add_item(self):
        ctx = SkillContext(
            trigger="tool_call",
            user_id=1,
            tool_name="my_tool",
            args={"action": "add", "item": "Test item"},
        )
        result = await MySkill().execute(ctx)
        assert result.success
        assert "Test item" in result.output
```

`tests/conftest.py` 中的测试 fixture 会自动为每个测试提供干净的 DB 和 skill schema。

## 提交前检查清单

- [ ] `SKILL.md` 包含 `name`、`description` 和工具定义
- [ ] `handler.py` 有 `Skill` 子类并实现了 `execute()`
- [ ] `__init__.py` 存在（可以为空）
- [ ] 如果用到 DB：`queries.py` 存在 + `init_schema()` 创建表
- [ ] 没有导入其他 skill（skill 之间完全隔离）
- [ ] 没有导入框架内部模块（heartbeat、ai_client 等） — 只能用 `mochi.skills.base`、`mochi.db`、`mochi.config`、`mochi.llm`
- [ ] 代码全英文（变量名、函数名、注释、docstring）
- [ ] 测试通过：`pytest tests/test_my_skill_handler.py`
- [ ] Bot 启动成功：日志中看到 `Registered skill: my_skill`
