"""Main discovers, authors, debugs, activates and uses a tool without opt-in."""

import json

import pytest

from mochi.ai_client import chat
from mochi.db import get_recent_tool_executions, get_tool_executions_for_turn, set_skill_enabled
from mochi.extensions import store
import mochi.skills as registry
from mochi.transport import IncomingMessage
from tests.e2e.mock_llm import make_response, make_tool_call


NAME = "local_reading"
SKILL_MD = """---
name: local_reading
description: "Personal reading log"
type: tool
---
## Tools
### local_reading_log (on_demand)
Add a book or list the extension's own reading log.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: add, list) | yes | Operation |
| title | string | no | Title for add |
"""

HANDLER = """import json
from mochi.skills.base import Skill, SkillResult

class ReadingSkill(Skill):
    async def execute(self, context):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "books.json"
        books = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if context.args["action"] == "add":
            title = context.args.get("title", "")
            if not title:
                return SkillResult(success=False, output="A title is required.")
            books.append(title)
            path.write_text(json.dumps(books), encoding="utf-8")
            return SkillResult(output="Saved " + title, state_changed=True)
        return SkillResult(output=json.dumps(books))
"""

SMOKE = """import asyncio
import json
import tempfile
from pathlib import Path
from mochi.extensions.loader import load_from_path
from mochi.skills.base import SkillContext

async def main():
    with tempfile.TemporaryDirectory() as directory:
        skill = load_from_path("local_reading", Path(__file__).parent,
                               data_dir=Path(directory))
        added = await skill.run(SkillContext(
            trigger="tool_call", user_id=1, tool_name="local_reading_log",
            args={"action": "add", "title": "Dune"}))
        assert added.success, added.output
        listed = await skill.run(SkillContext(
            trigger="tool_call", user_id=1, tool_name="local_reading_log",
            args={"action": "list"}))
        assert listed.success, listed.output
        assert json.loads(listed.output) == ["Dune"], listed.output
        print("Reading tool add/list passed with disposable data.")

asyncio.run(main())
"""


def _message(text):
    return IncomingMessage(
        user_id=1, channel_id=100, text=text, transport="fake", owner_authorized=True,
    )


def _call(name, args):
    return make_response(tool_calls=[make_tool_call(name, args)])


@pytest.mark.asyncio
async def test_main_finishes_development_and_uses_tool_without_owner_handoff(
    monkeypatch, mock_llm_factory,
):
    import mochi.config as config

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    monkeypatch.setattr(config, "TOOL_LOOP_MAX_ROUNDS", config.DEFAULT_TOOL_LOOP_MAX_ROUNDS)
    registry.discover()
    buggy_handler = HANDLER.replace("books.append(title)", "books.append(title.upper())")
    agent = mock_llm_factory([
        _call("request_tools", {"skills": ["development"]}),
        _call("inspect_extension", {"action": "guide", "extension_id": NAME}),
        _call("write_extension", {
            "action": "create", "extension_id": NAME,
            "skill_md": SKILL_MD, "handler_py": buggy_handler, "smoke_py": SMOKE,
        }),
        _call("run_extension", {"extension_id": NAME}),
        _call("write_extension", {
            "action": "edit", "extension_id": NAME, "path": "handler.py",
            "old_text": "books.append(title.upper())", "new_text": "books.append(title)",
        }),
        _call("run_extension", {"extension_id": NAME}),
        _call("activate_extension", {"extension_id": NAME}),
        _call("request_tools", {"skills": [NAME]}),
        _call("local_reading_log", {"action": "add", "title": "The Hobbit"}),
        _call("local_reading_log", {"action": "list"}),
        make_response("Created the reading tool and recorded The Hobbit."),
    ])
    reply = await chat(_message(
        "Build a personal reading log, fix any problems, "
        "record The Hobbit and show me the result.",
    ))
    assert reply.text
    assert len(agent.call_log) == 11
    assert "Develop personal tools" in agent.call_log[0]["messages"][0]["content"]
    assert "write_extension" not in {t["function"]["name"] for t in agent.call_log[0]["tools"]}
    assert "write_extension" in {t["function"]["name"] for t in agent.call_log[1]["tools"]}
    assert "local_reading_log" not in {t["function"]["name"] for t in agent.call_log[7]["tools"]}
    assert "local_reading_log" in {t["function"]["name"] for t in agent.call_log[8]["tools"]}
    assert "AssertionError" in str(agent.call_log[4]["messages"])
    assert "Reading tool add/list passed" in str(agent.call_log[6]["messages"])
    assert registry.get_skill(NAME) is not None
    assert "Saved The Hobbit" in str(agent.call_log[-1]["messages"])
    assert (store.extension_root(NAME) / "current" / "handler.py").is_file()

    records = get_recent_tool_executions(1, limit=20)
    authored = next(r for r in records if r["tool_name"] == "write_extension"
                    and r["action"] == "create")
    assert authored["arguments"]["handler_py"] == "[REDACTED]"
    assert authored["arguments"]["skill_md"] == "[REDACTED]"
    assert authored["arguments"]["smoke_py"] == "[REDACTED]"
    assert all("books.append" not in r["result_summary"] for r in records)
    attempts = get_tool_executions_for_turn(authored["turn_id"])
    assert len(attempts) == 8
    assert all(r["tool_name"] != "toggle_skill" for r in attempts)
    assert {r["turn_id"] for r in records} == {authored["turn_id"]}
    assert any(r["tool_name"] == "run_extension" and r["status"] == "failed" for r in attempts)

    assert json.loads((store.extension_root(NAME) / "data" / "books.json").read_text(
        encoding="utf-8",
    )) == ["The Hobbit"]


@pytest.mark.asyncio
async def test_new_activation_cannot_authorize_same_provider_response(monkeypatch, mock_llm_factory):
    import mochi.config as config

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    set_skill_enabled("development", True)
    name = "local_not_yet"
    store.scaffold(name)
    agent = mock_llm_factory([
        _call("request_tools", {"skills": ["development"]}),
        make_response(tool_calls=[
            make_tool_call("activate_extension", {"extension_id": name}),
            make_tool_call(name + "_echo", {"text": "not authorized"}),
        ]),
        _call("request_tools", {"skills": [name]}),
        _call(name + "_echo", {"text": "authorized"}),
        make_response("The new namespace is now usable."),
    ])
    await chat(_message("Activate my completed tool and use it."))
    assert "tool_not_available_this_turn" in str(agent.call_log[2]["messages"])
    assert "authorized" in str(agent.call_log[-1]["messages"])


@pytest.mark.asyncio
async def test_round_dispatch_keeps_old_code_and_refreshes_updated_schema(monkeypatch, mock_llm_factory):
    import mochi.config as config
    from mochi.extensions import template

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    set_skill_enabled("development", True)
    name = "local_round"
    files = template.files(name)
    store.scaffold(name)
    registry.activate_extension(name)
    store.write_file(name, "SKILL.md", files["SKILL.md"].replace("| text |", "| value |"))
    store.write_file(name, "handler.py", files["handler.py"].replace('get("text")', 'get("value")'))
    agent = mock_llm_factory([
        _call("request_tools", {"skills": ["development", name]}),
        make_response(tool_calls=[
            make_tool_call("activate_extension", {"extension_id": name}),
            make_tool_call(name + "_echo", {"text": "old round code"}),
        ]),
        _call(name + "_echo", {"value": "new round code"}),
        make_response("Updated without interrupting existing work."),
    ])
    result = await chat(_message("Activate the updated tool and use it."))
    attempts = get_tool_executions_for_turn(result._pending_history["turn_id"])
    assert len(attempts) == 3
    assert all(item["status"] == "success" for item in attempts)
    assert "old round code" in str(agent.call_log[2]["messages"])
    assert "new round code" in str(agent.call_log[-1]["messages"])
    schema = next(t for t in agent.call_log[2]["tools"] if t["function"]["name"] == name + "_echo")
    assert "value" in schema["function"]["parameters"]["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_exact_tool", [False, True])
async def test_new_resident_tool_can_be_requested_mid_turn(monkeypatch, mock_llm_factory, request_exact_tool):
    import mochi.config as config
    from mochi.extensions import template

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    set_skill_enabled("development", True)
    name = "local_resident"
    store.scaffold(name, skill_md=template.files(name)["SKILL.md"].replace("(on_demand)", "(resident)"))
    agent = mock_llm_factory([
        _call("request_tools", {"skills": ["development"]}),
        _call("activate_extension", {"extension_id": name}),
        _call("request_tools", {"skills": [name + "_echo" if request_exact_tool else name]}),
        _call(name + "_echo", {"text": "resident now"}),
        make_response("The newly installed tool returned resident now."),
    ])
    result = await chat(_message("Activate and use the new resident tool."))
    attempts = get_tool_executions_for_turn(result._pending_history["turn_id"])
    assert len(attempts) == 2
    assert all(item["status"] == "success" for item in attempts)
    assert name + "_echo" in {t["function"]["name"] for t in agent.call_log[3]["tools"]}


@pytest.mark.asyncio
async def test_requested_then_removed_tool_is_not_advertised_next_round(monkeypatch, mock_llm_factory):
    import mochi.config as config
    from mochi.extensions import template

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    set_skill_enabled("development", True)
    name = "local_removed"
    files = template.files(name)
    store.scaffold(name)
    registry.activate_extension(name)
    store.write_file(name, "SKILL.md", files["SKILL.md"].replace(name + "_echo", name + "_new"))
    store.write_file(name, "handler.py", files["handler.py"].replace(name + "_echo", name + "_new"))
    agent = mock_llm_factory([
        _call("request_tools", {"skills": ["development"]}),
        make_response(tool_calls=[
            make_tool_call("request_tools", {"skills": [name]}),
            make_tool_call("activate_extension", {"extension_id": name}),
        ]),
        _call("request_tools", {"skills": [name]}),
        _call(name + "_new", {"text": "new tool"}),
        make_response("Only the updated tool is advertised."),
    ])
    result = await chat(_message("Update my tool and use the new one."))
    tools = {t["function"]["name"] for t in agent.call_log[2]["tools"]}
    assert name + "_echo" not in tools
    assert name + "_new" not in tools
    attempts = get_tool_executions_for_turn(result._pending_history["turn_id"])
    assert len(attempts) == 2
    assert all(item["status"] == "success" for item in attempts)


@pytest.mark.asyncio
async def test_development_off_and_same_round_request_do_not_authorize_writes(
    monkeypatch, mock_llm_factory,
):
    import mochi.config as config

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    set_skill_enabled("development", False)
    registry.discover()
    disabled = mock_llm_factory([
        _call("request_tools", {"skills": ["development"]}),
        make_response("Development has been switched off."),
    ])
    await chat(_message("Make a tool."))
    assert not any(
        tool["function"]["name"] == "write_extension"
        for call in disabled.call_log for tool in call["tools"]
    )
    set_skill_enabled("development", True)
    mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call("request_tools", {"skills": ["development"]}),
            make_tool_call("write_extension", {
                "action": "create", "extension_id": "local_too_early",
            }),
        ]),
        make_response("The tool becomes available only after the request round."),
    ])
    await chat(_message("Try the newly requested tool."))
    assert not store.extension_root("local_too_early").exists()
