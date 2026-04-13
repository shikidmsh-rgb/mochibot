"""My skill handler — execute logic only. Tool defs in SKILL.md."""

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.my_skill.queries import create_item, get_items, delete_item


class MySkill(Skill):

    def init_schema(self, conn) -> None:
        """Create DB tables. Called once at startup."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS my_items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                content    TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            );
        """)

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = args.get("action")
        uid = context.user_id

        if action == "add":
            content = args.get("content", "")
            if not content:
                return SkillResult(output="Error: 'content' is required.", success=False)
            item_id = create_item(uid, content)
            return SkillResult(output=f"Item #{item_id} added: '{content}'.")

        elif action == "list":
            items = get_items(uid)
            if not items:
                return SkillResult(output="No items found.")
            lines = [f"#{it['id']} {it['content']}" for it in items]
            return SkillResult(output="\n".join(lines))

        elif action == "delete":
            item_id = args.get("item_id")
            if not item_id:
                return SkillResult(output="Error: 'item_id' is required.", success=False)
            ok = delete_item(uid, item_id)
            if ok:
                return SkillResult(output=f"Item #{item_id} deleted.")
            return SkillResult(output=f"Item #{item_id} not found.", success=False)

        return SkillResult(output=f"Unknown action: {action}", success=False)
