# MochiBot Examples

## Minimal Bot (just chat, no heartbeat)

```python
import asyncio
from mochi.db import init_db
from mochi.skills import registry
from mochi.ai_client import chat
from mochi.transport import IncomingMessage

async def main():
    init_db()
    registry.discover()
    
    # Simulate a conversation
    msg = IncomingMessage(user_id=1, channel_id=1, text="Hi! I love hiking.", transport="cli")
    response = await chat(msg)
    print(f"Bot: {response}")
    
    msg2 = IncomingMessage(user_id=1, channel_id=1, text="What do you know about me?", transport="cli")
    response2 = await chat(msg2)
    print(f"Bot: {response2}")

asyncio.run(main())
```

## Full Bot with Telegram + Heartbeat

```bash
# Just run main:
python -m mochi.main
```

## Custom Skill Example

See `mochi/skills/todo/` for a complete example of a skill with CRUD operations.
