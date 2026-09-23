import pytest

import mochi.conversation_summary as summary_worker
from mochi.db import get_conversation_summary_status, save_message
from mochi.llm import LLMResponse


class Client:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, LLMResponse):
            return output
        return LLMResponse(content=output, model="lite-test")


def _save_turns(count):
    for number in range(count):
        turn_id = f"turn-{number}"
        save_message(1, "user", f"user-{number}", turn_id=turn_id)
        save_message(1, "assistant", f"assistant-{number}", turn_id=turn_id)


@pytest.fixture(autouse=True)
def summary_state(monkeypatch):
    monkeypatch.setattr(summary_worker, "SUMMARY_BATCH_SIZE", 2)
    summary_worker._tasks.clear()


@pytest.mark.asyncio
async def test_summary_advances_in_complete_batches(monkeypatch):
    client = Client(["summary"])
    monkeypatch.setattr(
        summary_worker, "get_client_for_tier", lambda _tier: client,
    )
    _save_turns(2)

    await summary_worker.schedule_conversation_summary(1)

    status = get_conversation_summary_status(1, 2)
    assert status["summary"] == "summary"
    assert status["pending_turns"] == 0


@pytest.mark.asyncio
async def test_summary_failure_retries_same_batch(monkeypatch):
    _save_turns(2)
    failed = Client([RuntimeError("offline")])
    monkeypatch.setattr(
        summary_worker, "get_client_for_tier", lambda _tier: failed,
    )
    await summary_worker.schedule_conversation_summary(1)
    assert get_conversation_summary_status(1, 2)["pending_turns"] == 2

    recovered = Client(["recovered"])
    monkeypatch.setattr(
        summary_worker, "get_client_for_tier", lambda _tier: recovered,
    )
    await summary_worker.schedule_conversation_summary(1)
    assert recovered.calls[0]["messages"][1]["content"] == (
        failed.calls[0]["messages"][1]["content"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_fails", [False, True])
async def test_truncated_summary_is_not_saved_or_skipped(monkeypatch, retry_fails):
    _save_turns(2)
    client = Client([
        LLMResponse(content="incomplete", finish_reason="length"),
        LLMResponse(
            content="Still unfinished" if retry_fails else "Complete summary.",
            finish_reason="length" if retry_fails else "stop",
        ),
    ])
    monkeypatch.setattr(summary_worker, "get_client_for_tier", lambda _tier: client)
    await summary_worker.schedule_conversation_summary(1)
    status = get_conversation_summary_status(1, 2)
    assert status["pending_turns"] == (2 if retry_fails else 0)
    assert status["summary"] == ("" if retry_fails else "Complete summary.")
    assert len(client.calls) == 2
    assert client.calls[0]["max_tokens"] >= 1200
    assert client.calls[0]["messages"][1] == client.calls[1]["messages"][1]


@pytest.mark.asyncio
async def test_summary_recompresses_over_budget_without_cutting_text(monkeypatch):
    import mochi.config as config
    monkeypatch.setattr(config, "CONV_SUMMARY_MAX_TOKENS", 20)
    client = Client(["word " * 100, "A complete thought."])
    monkeypatch.setattr(summary_worker, "get_client_for_tier", lambda _tier: client)
    _save_turns(2)
    await summary_worker.schedule_conversation_summary(1)
    assert get_conversation_summary_status(1, 2)["summary"] == "A complete thought."
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_large_summary_input_reduces_complete_batches(monkeypatch):
    import mochi.config as config
    monkeypatch.setattr(config, "CONV_SUMMARY_MAX_TOKENS", 20)
    for number in range(2):
        save_message(1, "user", f"topic-{number} " + "large " * 8000, turn_id=f"large-{number}")
        save_message(1, "assistant", "Reply.", turn_id=f"large-{number}")
    client = Client(["First complete turn.", "Both complete turns."])
    monkeypatch.setattr(summary_worker, "get_client_for_tier", lambda _tier: client)
    await summary_worker.schedule_conversation_summary(1)
    assert get_conversation_summary_status(1, 2)["pending_turns"] == 0
    assert len(client.calls) == 2
    assert "topic-0" in client.calls[0]["messages"][1]["content"]
    assert "topic-1" not in client.calls[0]["messages"][1]["content"]
    assert "topic-1" in client.calls[1]["messages"][1]["content"]
