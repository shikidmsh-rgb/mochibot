"""E2E tests for the request_tools escalation loop.

Verifies the full LLM ↔ ai_client tool-loop interaction:
- Failed escalation returns full skill catalog (available_skills) without burning budget
- Successful escalation adds tools and increments counter
- Limit is enforced only on successful escalations
"""

import json

import pytest

from mochi.transport import IncomingMessage
from mochi.ai_client import chat
from tests.e2e.mock_llm import make_response, make_tool_call


def _msg(text: str, user_id: int = 1, channel_id: int = 100) -> IncomingMessage:
    return IncomingMessage(
        user_id=user_id, channel_id=channel_id,
        text=text, transport="fake",
    )


@pytest.fixture
def enable_router_and_escalation(monkeypatch):
    """Enable both TOOL_ROUTER and TOOL_ESCALATION; bypass router classification."""
    import mochi.config as cfg
    import mochi.tool_router as router_mod
    monkeypatch.setattr(cfg, "TOOL_ROUTER_ENABLED", True)
    monkeypatch.setattr(cfg, "TOOL_ESCALATION_ENABLED", True)

    # Force skill metadata init — other unit-test fixtures may have stubbed
    # _SKILL_DESCRIPTIONS to {} and set _metadata_initialized=True.
    monkeypatch.setattr(router_mod, "_metadata_initialized", False)
    router_mod._ensure_skill_metadata()

    # Stub the router classifier to return empty (forcing escalation path)
    async def _no_skills(*args, **kwargs):
        return []
    monkeypatch.setattr(router_mod, "classify_skills", _no_skills)


def _all_tool_messages(call_log: list[dict]) -> list[dict]:
    """Collect all unique tool messages across all LLM calls.

    NOTE: mock_llm stores message-list references (not copies), so every entry
    in call_log points to the same final list. We dedupe by tool_call_id.
    """
    if not call_log:
        return []
    seen_ids: set[str] = set()
    unique: list[dict] = []
    for c in call_log:
        for m in c.get("messages", []):
            if m.get("role") != "tool":
                continue
            tcid = m.get("tool_call_id")
            if tcid in seen_ids:
                continue
            seen_ids.add(tcid)
            unique.append(m)
    return unique


def _last_tool_message(call_log: list[dict]) -> dict:
    msgs = _all_tool_messages(call_log)
    return msgs[-1] if msgs else {}


class TestEscalationFailureReturnsCatalog:
    """When LLM requests an unknown skill, it gets back the full skill catalog."""

    @pytest.mark.asyncio
    async def test_unknown_skill_returns_available_skills(
        self, mock_llm_factory, enable_router_and_escalation
    ):
        mock = mock_llm_factory([
            # Round 1: LLM requests a non-existent skill
            make_response(tool_calls=[
                make_tool_call("request_tools", {
                    "skills": ["totally_made_up_skill"],
                    "reason": "guessing",
                }),
            ]),
            # Round 2: LLM gives up gracefully (we just need the loop to terminate)
            make_response("ok done"),
        ])

        await chat(_msg("hello"))

        # Round 2 LLM call should have received a JSON tool message containing
        # the available_skills catalog so the LLM could have retried.
        tool_msg = _last_tool_message(mock.call_log)
        assert tool_msg, "expected a tool message in the second LLM call"
        payload = json.loads(tool_msg["content"])
        assert payload["loaded"] == []
        assert "totally_made_up_skill" in payload["unknown"]
        assert "available_skills" in payload
        assert isinstance(payload["available_skills"], dict)
        # Must list at least some real registered skills
        assert "skill_management" in payload["available_skills"]
        assert "hint" in payload


class TestEscalationSuccessLoadsTools:
    """A valid skill request loads tools and the LLM can call them next round."""

    @pytest.mark.asyncio
    async def test_request_skill_then_call_tool(
        self, mock_llm_factory, enable_router_and_escalation
    ):
        mock = mock_llm_factory([
            # Round 1: request the skill
            make_response(tool_calls=[
                make_tool_call("request_tools", {
                    "skills": ["skill_management"],
                    "reason": "user asked what I can do",
                }),
            ]),
            # Round 2: now actually call list_skills
            make_response(tool_calls=[
                make_tool_call("list_skills", {}),
            ]),
            # Round 3: final reply
            make_response("Here are my skills: ..."),
        ])

        reply = await chat(_msg("what can you do?"))

        # Find the request_tools result (first tool message)
        all_tools = _all_tool_messages(mock.call_log)
        assert len(all_tools) >= 2, f"expected >=2 tool messages, got {len(all_tools)}"

        request_tools_result = all_tools[0]
        payload = json.loads(request_tools_result["content"])
        assert payload["loaded"] == ["skill_management"]
        assert "list_skills" in payload["tools_added"]

        # list_skills result should follow
        list_skills_result = all_tools[1]
        assert "Registered skills" in list_skills_result["content"]

        assert reply.text == "Here are my skills: ..."


class TestEscalationBudget:
    """Failed escalations don't burn budget; only successful ones count."""

    @pytest.mark.asyncio
    async def test_failed_escalation_does_not_count(
        self, mock_llm_factory, enable_router_and_escalation, monkeypatch
    ):
        """3 failed retries followed by a success — success should still go through."""
        import mochi.config as cfg
        # Set limit to 1 so we can prove failures don't consume it
        monkeypatch.setattr(cfg, "TOOL_ESCALATION_MAX_PER_TURN", 1)

        mock = mock_llm_factory([
            # 3 failed requests
            make_response(tool_calls=[make_tool_call("request_tools", {"skills": ["bad1"]})]),
            make_response(tool_calls=[make_tool_call("request_tools", {"skills": ["bad2"]})]),
            make_response(tool_calls=[make_tool_call("request_tools", {"skills": ["bad3"]})]),
            # Then a valid one — should still succeed because limit is 1 and counter is 0
            make_response(tool_calls=[make_tool_call("request_tools", {"skills": ["skill_management"]})]),
            # Final reply
            make_response("done"),
        ])

        await chat(_msg("hello"))

        # The 4th request_tools call (index 3 across all tool messages) should
        # report a successful load — the 3 prior failures must NOT have burnt budget.
        all_tools = _all_tool_messages(mock.call_log)
        assert len(all_tools) == 4, f"expected 4 tool messages, got {len(all_tools)}"
        # First 3 are failed escalations
        for i in range(3):
            payload = json.loads(all_tools[i]["content"])
            assert payload.get("loaded") == [], f"call {i} should be failure"
            assert "available_skills" in payload
        # 4th is the successful one
        last_payload = json.loads(all_tools[3]["content"])
        assert last_payload.get("loaded") == ["skill_management"], \
            f"expected successful load, got {last_payload}"

    @pytest.mark.asyncio
    async def test_limit_enforced_on_successes(
        self, mock_llm_factory, enable_router_and_escalation, monkeypatch
    ):
        """After N successful escalations, the next success request returns limit error."""
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "TOOL_ESCALATION_MAX_PER_TURN", 1)

        mock = mock_llm_factory([
            # 1st success — uses up the budget
            make_response(tool_calls=[make_tool_call("request_tools", {"skills": ["skill_management"]})]),
            # 2nd success request — should be rejected with limit error
            make_response(tool_calls=[make_tool_call("request_tools", {"skills": ["memory"]})]),
            # Final reply
            make_response("done"),
        ])

        await chat(_msg("hello"))

        # 1st request → success, 2nd → limit error
        all_tools = _all_tool_messages(mock.call_log)
        assert len(all_tools) == 2, f"expected 2 tool messages, got {len(all_tools)}"

        first = json.loads(all_tools[0]["content"])
        assert first.get("loaded") == ["skill_management"]

        second = json.loads(all_tools[1]["content"])
        assert "error" in second
        assert "limit" in second["error"].lower()
