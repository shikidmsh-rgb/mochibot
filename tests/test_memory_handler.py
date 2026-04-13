"""Tests for the memory skill handler — save, recall, update core, list, delete, stats, trash."""

import pytest
from unittest.mock import patch, MagicMock

from mochi.skills.base import SkillContext, SkillResult
from mochi.skills.memory.handler import MemorySkill
from mochi.db import (
    save_memory_item,
    get_core_memory,
    update_core_memory,
    delete_memory_items,
)


def _ctx(tool_name, user_id=1, **args):
    """Helper to build a SkillContext for memory tests."""
    return SkillContext(
        trigger="tool_call",
        user_id=user_id,
        channel_id=100,
        tool_name=tool_name,
        args=args,
    )


class TestSaveMemory:

    @pytest.mark.asyncio
    async def test_save_success(self):
        """Saving a memory item returns confirmation with ID."""
        ctx = _ctx("save_memory", content="User likes cats", category="preference")
        result = await MemorySkill().execute(ctx)
        assert result.success
        assert "Saved memory" in result.output
        assert "User likes cats" in result.output

    @pytest.mark.asyncio
    async def test_save_empty_content(self):
        """Saving with empty content is rejected."""
        ctx = _ctx("save_memory", content="")
        result = await MemorySkill().execute(ctx)
        assert not result.success
        assert "Nothing to save" in result.output

    @pytest.mark.asyncio
    async def test_save_default_category(self):
        """Saving without category defaults to 'general'."""
        ctx = _ctx("save_memory", content="Random fact")
        result = await MemorySkill().execute(ctx)
        assert result.success


class TestRecallMemory:

    @pytest.mark.asyncio
    async def test_recall_with_results(self):
        """Recalls saved memories matching a query."""
        save_memory_item(1, category="fact", content="User birthday is Jan 1")
        save_memory_item(1, category="fact", content="User birthday party was fun")
        ctx = _ctx("recall_memory", query="birthday")
        # Patch model_pool to avoid needing a real embedding model
        with patch("mochi.model_pool.get_pool") as mock_pool:
            mock_pool.return_value.embed.return_value = None
            result = await MemorySkill().execute(ctx)
        assert result.success
        assert "birthday" in result.output.lower()

    @pytest.mark.asyncio
    async def test_recall_no_results(self):
        """Recall with no matches returns 'No matching memories'."""
        ctx = _ctx("recall_memory", query="nonexistent_topic_xyz")
        with patch("mochi.model_pool.get_pool") as mock_pool:
            mock_pool.return_value.embed.return_value = None
            result = await MemorySkill().execute(ctx)
        assert "No matching memories" in result.output

    @pytest.mark.asyncio
    async def test_recall_with_embedding(self):
        """Recall attempts to generate embedding for vector search."""
        save_memory_item(1, category="fact", content="Important fact")
        ctx = _ctx("recall_memory", query="important")
        with patch("mochi.model_pool.get_pool") as mock_pool:
            mock_pool.return_value.embed.return_value = b"\x00" * 128
            result = await MemorySkill().execute(ctx)
        # Should still return results even with embedding
        assert result.success


class TestUpdateCoreMemory:

    @pytest.mark.asyncio
    async def test_add_to_core(self):
        """Adding to core memory appends a bullet line."""
        ctx = _ctx("update_core_memory", action="add", content="Loves cooking")
        result = await MemorySkill().execute(ctx)
        assert result.success
        assert "added" in result.output.lower()
        core = get_core_memory(1)
        assert "Loves cooking" in core

    @pytest.mark.asyncio
    async def test_delete_from_core(self):
        """Deleting matching lines from core memory."""
        update_core_memory(1, "- Loves cooking\n- Hates rain\n- Loves music")
        ctx = _ctx("update_core_memory", action="delete", content="cooking")
        result = await MemorySkill().execute(ctx)
        assert result.success
        assert "deleted" in result.output.lower()
        core = get_core_memory(1)
        assert "cooking" not in core
        assert "rain" in core

    @pytest.mark.asyncio
    async def test_delete_no_match(self):
        """Deleting with no matching line reports 'no line matching'."""
        update_core_memory(1, "- Fact A\n- Fact B")
        ctx = _ctx("update_core_memory", action="delete", content="nonexistent")
        result = await MemorySkill().execute(ctx)
        assert "no line matching" in result.output.lower()

    @pytest.mark.asyncio
    async def test_add_empty_content(self):
        """Adding empty content is rejected."""
        ctx = _ctx("update_core_memory", action="add", content="")
        result = await MemorySkill().execute(ctx)
        assert not result.success


class TestListMemories:

    @pytest.mark.asyncio
    async def test_list_with_items(self):
        """List returns saved memories."""
        save_memory_item(1, category="pref", content="Likes blue")
        save_memory_item(1, category="pref", content="Likes green")
        ctx = _ctx("list_memories", category="pref")
        result = await MemorySkill().execute(ctx)
        assert result.success
        assert "blue" in result.output.lower()
        assert "green" in result.output.lower()

    @pytest.mark.asyncio
    async def test_list_empty(self):
        """List with no memories returns 'No memories found'."""
        ctx = _ctx("list_memories")
        result = await MemorySkill().execute(ctx)
        assert "No memories found" in result.output


class TestDeleteMemory:

    @pytest.mark.asyncio
    async def test_delete_success(self):
        """Deleting a memory moves it to trash."""
        mid = save_memory_item(1, category="temp", content="Delete me")
        ctx = _ctx("delete_memory", memory_id=mid)
        result = await MemorySkill().execute(ctx)
        assert result.success
        assert "trash" in result.output.lower()

    @pytest.mark.asyncio
    async def test_delete_not_found(self):
        """Deleting a nonexistent memory returns error."""
        ctx = _ctx("delete_memory", memory_id=99999)
        result = await MemorySkill().execute(ctx)
        assert not result.success
        assert "not found" in result.output.lower()


class TestMemoryStats:

    @pytest.mark.asyncio
    async def test_stats_returns_info(self):
        """Stats returns total count and category breakdown."""
        save_memory_item(1, category="fact", content="A")
        save_memory_item(1, category="pref", content="B")
        ctx = _ctx("memory_stats")
        result = await MemorySkill().execute(ctx)
        assert result.success
        assert "Total memories" in result.output


class TestMemoryTrashBin:

    @pytest.mark.asyncio
    async def test_trash_list_empty(self):
        """Empty trash returns 'Trash is empty'."""
        ctx = _ctx("memory_trash_bin", action="list")
        result = await MemorySkill().execute(ctx)
        assert "empty" in result.output.lower()

    @pytest.mark.asyncio
    async def test_trash_list_with_items(self):
        """Trash lists deleted items."""
        mid = save_memory_item(1, category="temp", content="Trashed item")
        delete_memory_items([mid], deleted_by="user")
        ctx = _ctx("memory_trash_bin", action="list")
        result = await MemorySkill().execute(ctx)
        assert result.success
        assert "Trashed item" in result.output

    @pytest.mark.asyncio
    async def test_trash_restore(self):
        """Restoring from trash creates a new memory."""
        mid = save_memory_item(1, category="keep", content="Restore me")
        delete_memory_items([mid], deleted_by="user")
        # Get the trash ID
        from mochi.db import list_memory_trash
        trash = list_memory_trash(1)
        assert len(trash) >= 1
        tid = trash[0]["id"]
        ctx = _ctx("memory_trash_bin", action="restore", trash_id=tid)
        result = await MemorySkill().execute(ctx)
        assert result.success
        assert "Restored" in result.output


class TestViewCoreMemory:

    @pytest.mark.asyncio
    async def test_view_core_empty(self):
        """Viewing empty core memory returns appropriate message."""
        ctx = _ctx("view_core_memory")
        result = await MemorySkill().execute(ctx)
        assert "empty" in result.output.lower()

    @pytest.mark.asyncio
    async def test_view_core_with_content(self):
        """Viewing core memory shows its contents."""
        update_core_memory(1, "- Important fact")
        ctx = _ctx("view_core_memory")
        result = await MemorySkill().execute(ctx)
        assert result.success
        assert "Important fact" in result.output
