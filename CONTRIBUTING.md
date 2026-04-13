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
keywords: [my_keyword, 我的关键词]
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
7. **`keywords` 必须高精度** — 只放能明确指向该 skill 的关键词
8. **平台不兼容时声明 `exclude_transports`** — 如果 skill 依赖特定平台特性（如 Telegram file_id），在 SKILL.md 中用 `exclude_transports: [wechat]` 排除不兼容平台。不确定时不要排除
9. **代码全英文** — 变量名、函数名、注释、docstring 一律英文

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

## 代码风格

- **代码**：全英文（变量名、函数名、注释、docstring）
- **Commit messages**：英文，[conventional commits](https://www.conventionalcommits.org/)
- 参见 [ARCHITECTURE.md](ARCHITECTURE.md) 了解分层规则和依赖方向
