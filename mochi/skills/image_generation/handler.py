"""Generate an in-memory image for the current chat turn."""

from mochi.image_service import generate_image
from mochi.skills.base import Skill, SkillContext, SkillResult


class ImageGenerationSkill(Skill):
    async def execute(self, context: SkillContext) -> SkillResult:
        if context.tool_name != "generate_and_send_image":
            return SkillResult(output="Unknown image tool", success=False)
        if (
            context.actor != "main" or context.source != "chat"
            or context.transport != "wechat" or not context.owner_authorized
        ):
            return SkillResult(
                output="当前无法发送图片；未调用生图服务。",
                success=False, error_code="image_unavailable", retryable=False,
            )
        image = await generate_image(context.args["prompt"])
        return SkillResult(image=image)
