import pytest

from mochi import core_store
from mochi.diary import diary
from mochi.skills.base import SkillContext
from mochi.skills.workspace.handler import WorkspaceSkill


@pytest.mark.asyncio
async def test_core_and_diary_reject_stale_revisions_without_losing_new_text():
    core_store.replace_core("Original Core.")
    day, original_journal, _ = diary.read_write_snapshot()
    core_store.replace_core_exact(expected_content="Original Core.", content="Concurrent Core.")
    diary.replace_section_exact(
        "今日日記", expected_content=original_journal, content="Concurrent journal.", target_date=day,
    )
    with pytest.raises(core_store.CoreConflictError):
        core_store.replace_core_exact(expected_content="Original Core.", content="Stale Core.")
    context = SkillContext(trigger="tool_call", user_id=1, tool_name="write_diary", args={
        "content": "Stale journal.", "_expected_content": original_journal,
        "_source_date": day, "_target_date": day,
    })
    rejected = await WorkspaceSkill().execute(context)
    assert not rejected.success and not rejected.state_changed
    assert core_store.read_core() == "Concurrent Core."
    assert diary.read("今日日記") == rejected.document_snapshot == "Concurrent journal."
    core_store.replace_core_exact(expected_content="Concurrent Core.", content="Final Core.")
    context.args.update(content="Final journal.", _expected_content=rejected.document_snapshot)
    assert (await WorkspaceSkill().execute(context)).state_changed
    diary.rewrite_section("今日状態", ["New automatic status"])
    assert core_store.read_core() == "Final Core."
    assert diary.read("今日日記") == "Final journal."
