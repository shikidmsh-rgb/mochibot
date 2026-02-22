# Contributing to MochiBot

MochiBot is in early alpha. Contributions welcome!

1. Fork the repo
2. Create a feature branch
3. Follow the architecture in [ARCHITECTURE.md](ARCHITECTURE.md)
4. Submit a PR

---

## Adding a Custom Skill

Skills are active capabilities the Chat model can invoke via tool calls.

### Structure

```
mochi/skills/my_skill/
├── __init__.py          # empty
├── SKILL.md             # tool definitions (parsed at startup)
└── handler.py           # skill logic
```

### SKILL.md

```markdown
---
name: my_skill
expose: true
triggers: [tool_call]
---

## Tool: my_tool
Description: Does something cool

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| input | string | yes | The input to process |
```

### handler.py

```python
from mochi.skills.base import Skill, SkillContext, SkillResult

class MySkillHandler(Skill):
    async def execute(self, context: SkillContext) -> SkillResult:
        input_text = context.args.get("input", "")
        # Your logic here
        return SkillResult(output=f"Processed: {input_text}")
```

Restart MochiBot → check logs for `✅ Registered skill: my_skill`.

To disable: rename `SKILL.md` → `SKILL.md.disabled`.

---

## Adding a Custom Observer

Observers are passive, read-only sensors that feed context into the Heartbeat Think step — zero LLM calls.

### Structure

```
mochi/observers/my_observer/
├── __init__.py          # empty
├── OBSERVATION.md       # metadata + interval
└── observer.py          # collection logic
```

### OBSERVATION.md

```markdown
---
name: my_observer
interval_minutes: 30
enabled: true
requires_config: [MY_API_KEY]
---

Description of what this observer watches.
```

### observer.py

```python
from mochi.observers.base import Observer

class MyObserver(Observer):
    async def observe(self) -> dict:
        # Your logic here — return {} to report nothing
        return {"value": 42}
```

### Key behaviors

- **Auto-disabled** if any `requires_config` env var is missing at startup
- **Interval-throttled**: `safe_observe()` skips the call if last run < `interval_minutes` ago
- **Error-isolated**: exceptions are caught and logged; returns `{}` on failure
- **5 consecutive failures** → auto-disabled for the session
- Return `{}` to silently skip a tick (e.g. API unavailable)

Restart MochiBot → check logs for `✅ Registered observer: my_observer`.

To disable: set `enabled: false` in `OBSERVATION.md`, or rename it `OBSERVATION.md.disabled`.

---

## Code Style

- **Comments**: English only
- **Commit messages**: English, [conventional commits](https://www.conventionalcommits.org/)
- **Naming**: English, camelCase for variables/functions
- See [ARCHITECTURE.md](ARCHITECTURE.md) for layer rules and dependency direction
