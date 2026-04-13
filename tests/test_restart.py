"""Tests for mochi.shutdown — restart event and flag file."""

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import patch

import mochi.shutdown as shutdown


@pytest.fixture(autouse=True)
def reset_shutdown_globals(monkeypatch, tmp_path):
    """Reset module globals and use a temp flag file."""
    monkeypatch.setattr(shutdown, "_restart_event", None)
    monkeypatch.setattr(shutdown, "_RESTART_FLAG", tmp_path / ".restart_requested")


def test_restart_exit_code_is_42():
    assert shutdown.RESTART_EXIT_CODE == 42


def test_init_restart_event():
    event = shutdown.init_restart_event()
    assert isinstance(event, asyncio.Event)
    assert not event.is_set()


def test_request_restart_sets_event():
    event = shutdown.init_restart_event()
    shutdown.request_restart(12345)
    assert event.is_set()


def test_request_restart_writes_flag(tmp_path):
    shutdown._RESTART_FLAG = tmp_path / ".restart_requested"
    shutdown.init_restart_event()
    shutdown.request_restart(12345)

    assert shutdown._RESTART_FLAG.exists()
    data = json.loads(shutdown._RESTART_FLAG.read_text())
    assert data["channel_id"] == 12345


def test_request_restart_before_init_is_safe():
    """Calling request_restart before init should not crash."""
    shutdown.request_restart(0)
    # No exception raised — the flag file is still written
    assert shutdown._RESTART_FLAG.exists()


def test_consume_restart_flag(tmp_path):
    flag = tmp_path / ".restart_requested"
    shutdown._RESTART_FLAG = flag
    flag.write_text(json.dumps({"channel_id": 999}))

    result = shutdown.consume_restart_flag()
    assert result == {"channel_id": 999}
    assert not flag.exists()  # file deleted


def test_consume_restart_flag_no_file():
    result = shutdown.consume_restart_flag()
    assert result is None


def test_consume_restart_flag_corrupt_json(tmp_path):
    flag = tmp_path / ".restart_requested"
    shutdown._RESTART_FLAG = flag
    flag.write_text("not json")

    result = shutdown.consume_restart_flag()
    assert result is None
    assert not flag.exists()  # file cleaned up
