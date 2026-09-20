import json

from mochi.skills.base import Skill, SkillContext, SkillResult

from .helpers import record_text


class LegacyCounter(Skill):
    def __init__(self):
        super().__init__()
        self.constructor_token = self.get_config("ACCESS_TOKEN")
        self.constructor_step = self.config["STEP"]
        self.constructor_data_exists = self.data_dir.is_dir()

    async def execute(self, context: SkillContext) -> SkillResult:
        mode = context.args.get("mode", "record")
        if mode == "reject":
            return SkillResult(
                output="Record rejected", success=False, summary="No record saved.",
                error_code="record_rejected", retryable=True,
            )
        path = self.data_dir / "counter.json"
        count = json.loads(path.read_text(encoding="utf-8"))["count"] if path.exists() else 0
        record = {
            "count": count + self.config["STEP"],
            "text": record_text(self.config["LABEL"], context.args["text"]),
            "actor": context.actor,
            "user_id": context.user_id,
        }
        path.write_text(json.dumps(record), encoding="utf-8")
        if mode == "crash":
            raise RuntimeError("Interrupted after saving")
        return SkillResult(
            output=json.dumps(record), summary="Saved one counter record.",
            entity_refs=[f"counter:{record['count']}"], state_changed=True,
            actions=[{"type": "message", "content": record["text"]}],
            content_source="agent_authored_document",
        )
