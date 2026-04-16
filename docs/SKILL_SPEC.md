# MochiBot Skill 开发规范

Skill 是自包含的功能模块。本文档涵盖创建一个 skill 所需的全部内容。

## 快速开始

```bash
# 1. 复制模板
cp -r docs/skill_template/ mochi/skills/my_skill/

# 2. 编辑 SKILL.md（工具定义）和 handler.py（逻辑）

# 3. 重启 MochiBot — 日志中应看到：
#    INFO  Registered skill: my_skill (type=tool, tools=['my_tool'], triggers=['tool_call'])

# 4. 打开 Admin Portal → Skills 页面 — 你的 skill 会自动出现，
#    带有开关、配置项等，无需任何前端代码改动。
```

> **Admin Portal 自动集成**：框架启动时会扫描 `mochi/skills/` 下所有包含 `handler.py` + `SKILL.md` 的目录，自动注册。Admin Portal 的 Skills 页面从注册表动态渲染 —— **不需要为新 skill 写任何 UI 代码**。你在 SKILL.md 中声明的元数据（name、description、type、tier、config 等）会直接反映到管理面板中。

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
multi_turn: false                 # true 表示 skill 可能需要多轮工具调用
core: false                       # true = 不可在管理面板中被禁用
always_on: false                  # true = 每轮都注入工具，不依赖 router 分类

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

Observer 让 heartbeat 能感知 skill 的状态。**大多数 skill 不需要 observer** — 只有当 heartbeat 的 Think 步骤需要知道你的 skill 数据时才添加。

### 什么时候需要？

| Skill 类型 | 例子 | 需要 observer? |
|------------|------|---------------|
| 被动工具型 — 用户调用才干活 | 翻译、搜索、笔记 | 不需要 |
| 有持续状态型 — heartbeat 应该知晓 | reminder、todo、健康数据 | 需要 |

**判断标准**：heartbeat 巡逻时需不需要看到这个 skill 的数据？不需要就别加。

### 文件结构

在 skill 目录下添加两个文件：

```
mochi/skills/my_skill/
├── observer.py        # Observer 子类
└── OBSERVATION.md     # 元数据（name、interval、fields）
```

并在 `SKILL.md` front-matter 中声明：

```yaml
sense:
  interval: 20    # Observer 运行间隔（分钟）
```

### observer.py 模板

```python
"""My Skill Observer — 一句话描述。"""

import logging
from mochi.observers.base import Observer

log = logging.getLogger(__name__)


class MySkillObserver(Observer):

    async def observe(self) -> dict:
        from mochi.config import OWNER_USER_ID
        from mochi.skills.my_skill.queries import some_query

        user_id = OWNER_USER_ID
        if not user_id:
            return {}

        # 查询数据，返回 flat dict。返回 {} 表示无数据可报告。
        data = some_query(user_id)
        return {"some_key": data}
```

### OBSERVATION.md 模板

```yaml
---
name: my_skill          # 必须与 SKILL.md 的 name 一致
interval: 20            # 采集间隔（分钟）
type: context           # source（外部 API）| context（内部 DB）
enabled: true
requires_config: []     # 需要的环境变量/DB 配置（无则留空）
skill_name: my_skill    # 关联到 skill toggle — 禁用 skill = 禁用此 observer
---

一句话描述。

## Fields
| Field | Type | Description |
|-------|------|-------------|
| some_key | int | 数据含义 |
```

### 关键规则

- **`name` 必须匹配**：`OBSERVATION.md` 的 `name` 字段是 observer registry 的 key，heartbeat 通过 `observation["observers"]["my_skill"]` 读取数据。名字不匹配 = 静默丢失数据。
- **`skill_name` 关联 toggle**：admin 禁用 skill 时，observer 也自动停止采集。
- **返回 `{}` 表示无数据**：空 dict 不会缓存，下次 tick 会重试。
- **observer 只读**：禁止发送消息、调用其他 skill、写入数据。纯数据采集。
- **`has_delta()` 可选重写**：默认 `prev != curr` 即触发 Think。如果你的数据变化不需要触发 Think（如天气），重写返回 `False`。

### 已有示例

| Observer | 位置 | 特点 |
|----------|------|------|
| weather | `mochi/skills/weather/observer.py` | 外部 API、`has_delta` 返回 False |
| oura | `mochi/skills/oura/observer.py` | 外部 API、带缓存、默认 delta |
| reminder | `mochi/skills/reminder/observer.py` | 纯 DB 查询、默认 delta |
| todo | `mochi/skills/todo/observer.py` | 纯 DB 查询、自定义 `has_delta` |

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
- [ ] 如果加了 observer：`observer.py` + `OBSERVATION.md` 存在，`SKILL.md` 有 `sense:` 字段，`OBSERVATION.md` 的 `name` 与 skill name 一致
- [ ] 代码全英文（变量名、函数名、注释、docstring）
- [ ] 测试通过：`pytest tests/test_my_skill_handler.py`
- [ ] Bot 启动成功：日志中看到 `Registered skill: my_skill`
- [ ] Admin Portal 验证：打开 Skills 页面，确认 skill 卡片正确显示（名称、描述、开关、配置项）
