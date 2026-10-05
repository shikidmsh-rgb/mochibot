import json

import pytest

from mochi import skills
from mochi.extensions import store
from mochi.skills.base import SkillContext
from mochi.skills.personal_workspace.handler import PersonalWorkspaceSkill


@pytest.mark.asyncio
async def test_workspace_rejects_escape_and_preserves_the_last_working_version():
    draft = "extensions/local_guide/draft"
    manifest = "---\nname: local_guide\nmod_api: 1\nkind: document\ndescription: Notes\ntype: tool\n---\n\n"

    async def call(tool, **args):
        return await PersonalWorkspaceSkill().execute(SkillContext(
            trigger="tool_call", actor="main", user_id=1, source="chat",
            tool_name=tool, args=args,
        ))

    created = await call("edit_workspace", action="create", path=draft, files=[
        {"path": "SKILL.md", "content": manifest + "Original notes."},
    ])
    assert created.success
    assert (await call("activate_extension", path=draft)).success
    current = skills.get_skill("local_guide")
    outside = store.ROOT / "outside.md"
    outside.write_text("Keep outside content.", encoding="utf-8")
    escape = await call(
        "edit_workspace", action="replace", path=f"{draft}/../../outside.md", content="Overwrite",
    )
    assert not escape.success
    assert outside.read_text(encoding="utf-8") == "Keep outside content."
    assert (await call(
        "edit_workspace", action="replace", path=f"{draft}/SKILL.md", content=manifest,
    )).success
    assert not (await call("activate_extension", path=draft)).success
    assert skills.get_skill("local_guide") is current
    assert json.loads((await skills.dispatch("local_guide_read", {})).output)["content"] == "Original notes."
    assert (await call(
        "edit_workspace", action="replace", path=f"{draft}/SKILL.md", content=manifest + "Revised notes.",
    )).success
    assert (await call("activate_extension", path=draft)).success
    assert json.loads((await skills.dispatch("local_guide_read", {})).output)["content"] == "Revised notes."
    assert (store.ROOT / "local_guide" / "previous" / "SKILL.md").read_text(
        encoding="utf-8",
    ) == manifest + "Original notes."
