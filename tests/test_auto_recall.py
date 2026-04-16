"""Tests for memory auto-recall (pre-turn embedding retrieval)."""

import time
import pytest
from unittest.mock import patch, MagicMock

from mochi.ai_client import (
    _retrieve_memories_for_turn,
    _build_system_prompt,
    _user_last_recall,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_recalled_item(content="User likes ramen", score=8.5, vec_sim=0.80,
                        category="preference", item_id=1):
    """Build a dict matching recall_memory() return shape."""
    return {
        "id": item_id,
        "category": category,
        "content": content,
        "importance": 2,
        "source": "auto",
        "created_at": "2026-04-01T10:00:00",
        "updated_at": "2026-04-10T12:00:00",
        "score": score,
        "vec_sim": vec_sim,
    }


_AUTO_RECALL_DEFAULTS = {
    "MEMORY_AUTO_RECALL": True,
    "MEMORY_AUTO_RECALL_TOP_K": 5,
    "MEMORY_AUTO_RECALL_MAX_ITEMS": 3,
    "MEMORY_AUTO_RECALL_MIN_VEC_SIM": 0.35,
    "MEMORY_AUTO_RECALL_MIN_SCORE": 0.72,
    "MEMORY_AUTO_RECALL_MAX_CHARS": 320,
    "MEMORY_AUTO_RECALL_COOLDOWN": 120,
}


def _patch_config(**overrides):
    """Patch mochi.config with auto-recall defaults + overrides."""
    values = {**_AUTO_RECALL_DEFAULTS, **overrides}

    def _side_effect(name, *args):
        if name in values:
            return values[name]
        raise ImportError(f"Unexpected import: {name}")

    import mochi.config as config_mod
    patches = {}
    for k, v in values.items():
        patches[k] = v
    return patch.multiple("mochi.config", **patches)


@pytest.fixture(autouse=True)
def _clear_cooldown():
    """Clear cooldown dict before each test."""
    _user_last_recall.clear()
    yield
    _user_last_recall.clear()


# ── Tests: _retrieve_memories_for_turn ───────────────────────────────────

class TestRetrieveMemories:

    def test_disabled_returns_empty(self):
        """MEMORY_AUTO_RECALL=false → returns []."""
        with _patch_config(MEMORY_AUTO_RECALL=False):
            result = _retrieve_memories_for_turn("hello", user_id=1)
        assert result == []

    def test_empty_text_returns_empty(self):
        """Empty message text → returns []."""
        with _patch_config():
            result = _retrieve_memories_for_turn("", user_id=1)
        assert result == []

    def test_no_user_id_returns_empty(self):
        """user_id=0 → returns []."""
        with _patch_config():
            result = _retrieve_memories_for_turn("hello", user_id=0)
        assert result == []

    def test_embed_none_returns_empty(self):
        """embed() returns None (no provider configured) → returns []."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = None
        with _patch_config(), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool):
            result = _retrieve_memories_for_turn("hello", user_id=1)
        assert result == []

    def test_successful_recall(self):
        """Happy path: embed + recall → filtered + formatted results."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = b"\x00" * 100

        items = [
            _make_recalled_item("User loves ramen", score=8.5, vec_sim=0.85, item_id=1),
            _make_recalled_item("User is allergic to peanuts", score=7.8, vec_sim=0.70, item_id=2),
            _make_recalled_item("User had a bad day", score=7.5, vec_sim=0.55, item_id=3),
        ]

        with _patch_config(), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool), \
             patch("mochi.ai_client.recall_memory", return_value=items):
            result = _retrieve_memories_for_turn("I'm hungry", user_id=1)

        assert len(result) == 3
        assert result[0]["text"] == "User loves ramen"
        assert result[0]["score"] == 0.85
        assert result[0]["category"] == "preference"
        assert result[0]["ts"] == "2026-04-10"

    def test_vec_sim_filter(self):
        """Items with vec_sim below threshold are excluded."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = b"\x00" * 100

        items = [
            _make_recalled_item("Good one", score=8.0, vec_sim=0.80, item_id=1),
            _make_recalled_item("Low sim", score=8.0, vec_sim=0.20, item_id=2),
        ]

        with _patch_config(), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool), \
             patch("mochi.ai_client.recall_memory", return_value=items):
            result = _retrieve_memories_for_turn("test", user_id=1)

        assert len(result) == 1
        assert result[0]["text"] == "Good one"

    def test_score_filter(self):
        """Items with normalized score below threshold are excluded."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = b"\x00" * 100

        items = [
            _make_recalled_item("High score", score=8.0, vec_sim=0.80, item_id=1),
            _make_recalled_item("Low score", score=5.0, vec_sim=0.80, item_id=2),  # 5.0/10 = 0.50 < 0.72
        ]

        with _patch_config(), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool), \
             patch("mochi.ai_client.recall_memory", return_value=items):
            result = _retrieve_memories_for_turn("test", user_id=1)

        assert len(result) == 1
        assert result[0]["text"] == "High score"

    def test_max_items_cap(self):
        """Only top MAX_ITEMS memories are returned."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = b"\x00" * 100

        items = [
            _make_recalled_item(f"Memory {i}", score=9.0, vec_sim=0.90, item_id=i)
            for i in range(10)
        ]

        with _patch_config(MEMORY_AUTO_RECALL_MAX_ITEMS=3), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool), \
             patch("mochi.ai_client.recall_memory", return_value=items):
            result = _retrieve_memories_for_turn("test", user_id=1)

        assert len(result) == 3

    def test_text_truncation(self):
        """Long memory content is truncated to MAX_CHARS."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = b"\x00" * 100

        long_content = "A" * 500
        items = [_make_recalled_item(long_content, score=9.0, vec_sim=0.90)]

        with _patch_config(MEMORY_AUTO_RECALL_MAX_CHARS=100), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool), \
             patch("mochi.ai_client.recall_memory", return_value=items):
            result = _retrieve_memories_for_turn("test", user_id=1)

        assert len(result) == 1
        assert len(result[0]["text"]) <= 100
        assert result[0]["text"].endswith("...")

    def test_exception_returns_empty(self):
        """Any exception during recall → returns [] (non-fatal)."""
        mock_pool = MagicMock()
        mock_pool.embed.side_effect = RuntimeError("embedding service down")

        with _patch_config(), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool):
            result = _retrieve_memories_for_turn("test", user_id=1)

        assert result == []


class TestCooldown:

    def test_cooldown_suppresses_second_call(self):
        """Second call within cooldown window returns []."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = b"\x00" * 100

        items = [_make_recalled_item("Memory", score=9.0, vec_sim=0.90)]

        with _patch_config(MEMORY_AUTO_RECALL_COOLDOWN=120), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool), \
             patch("mochi.ai_client.recall_memory", return_value=items):
            first = _retrieve_memories_for_turn("hello", user_id=1)
            second = _retrieve_memories_for_turn("world", user_id=1)

        assert len(first) == 1
        assert second == []

    def test_cooldown_expired_allows_recall(self):
        """After cooldown expires, recall runs again."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = b"\x00" * 100

        items = [_make_recalled_item("Memory", score=9.0, vec_sim=0.90)]

        with _patch_config(MEMORY_AUTO_RECALL_COOLDOWN=1), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool), \
             patch("mochi.ai_client.recall_memory", return_value=items):
            first = _retrieve_memories_for_turn("hello", user_id=1)
            time.sleep(1.1)
            second = _retrieve_memories_for_turn("world", user_id=1)

        assert len(first) == 1
        assert len(second) == 1

    def test_cooldown_zero_disables(self):
        """MEMORY_AUTO_RECALL_COOLDOWN=0 → no cooldown, both calls run."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = b"\x00" * 100

        items = [_make_recalled_item("Memory", score=9.0, vec_sim=0.90)]

        with _patch_config(MEMORY_AUTO_RECALL_COOLDOWN=0), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool), \
             patch("mochi.ai_client.recall_memory", return_value=items):
            first = _retrieve_memories_for_turn("hello", user_id=1)
            second = _retrieve_memories_for_turn("world", user_id=1)

        assert len(first) == 1
        assert len(second) == 1

    def test_different_users_independent_cooldown(self):
        """Cooldown is per-user — user 2 is not affected by user 1's cooldown."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = b"\x00" * 100

        items = [_make_recalled_item("Memory", score=9.0, vec_sim=0.90)]

        with _patch_config(MEMORY_AUTO_RECALL_COOLDOWN=120), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool), \
             patch("mochi.ai_client.recall_memory", return_value=items):
            result_user1 = _retrieve_memories_for_turn("hello", user_id=1)
            result_user2 = _retrieve_memories_for_turn("hello", user_id=2)

        assert len(result_user1) == 1
        assert len(result_user2) == 1


class TestBumpAccess:

    def test_auto_recall_passes_bump_access_false(self):
        """_retrieve_memories_for_turn calls recall_memory with bump_access=False."""
        mock_pool = MagicMock()
        mock_pool.embed.return_value = b"\x00" * 100

        with _patch_config(), \
             patch("mochi.model_pool.get_pool", return_value=mock_pool), \
             patch("mochi.ai_client.recall_memory", return_value=[]) as mock_recall:
            _retrieve_memories_for_turn("test", user_id=1)

        mock_recall.assert_called_once()
        call_kwargs = mock_recall.call_args
        assert call_kwargs.kwargs.get("bump_access") is False or \
               (len(call_kwargs.args) > 4 and call_kwargs.args[4] is False) or \
               call_kwargs[1].get("bump_access") is False


class TestBuildSystemPromptRecall:

    def test_recalled_memories_injected(self):
        """_build_system_prompt includes recalled memories section."""
        memories = [
            {"text": "User loves ramen", "score": 0.85, "ts": "2026-04-10", "category": "preference"},
            {"text": "User has a cat", "score": 0.78, "ts": "2026-04-08", "category": "fact"},
        ]
        with patch("mochi.ai_client.get_system_chat_modules",
                    return_value={"soul": "Test", "agent": "Test"}), \
             patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(user_id=1, recalled_memories=memories)

        assert "相关记忆" in prompt
        assert "User loves ramen" in prompt
        assert "User has a cat" in prompt
        # score is intentionally excluded from prompt (LLM doesn't need it)
        assert "score=" not in prompt
        assert "preference" in prompt
        assert "2026-04-10" in prompt

    def test_no_recalled_memories_no_section(self):
        """Empty recalled_memories → no 相关记忆 section."""
        with patch("mochi.ai_client.get_system_chat_modules",
                    return_value={"soul": "Test", "agent": "Test"}), \
             patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(user_id=1, recalled_memories=[])
        assert "相关记忆" not in prompt

    def test_none_recalled_memories_no_section(self):
        """None recalled_memories → no 相关记忆 section."""
        with patch("mochi.ai_client.get_system_chat_modules",
                    return_value={"soul": "Test", "agent": "Test"}), \
             patch("mochi.ai_client.get_prompt", return_value=""):
            prompt = _build_system_prompt(user_id=1, recalled_memories=None)
        assert "相关记忆" not in prompt
