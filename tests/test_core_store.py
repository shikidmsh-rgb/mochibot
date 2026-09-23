import pytest

import mochi.core_store as core_store


@pytest.mark.parametrize("content", [b"Personal Core\r\n", b""])
def test_initialization_preserves_existing_core(content):
    core_store.DATA_DIR.mkdir(parents=True)
    path = core_store.DATA_DIR / "core.md"
    path.write_bytes(content)

    core_store.initialize_core()
    assert core_store.read_core() == content.decode().strip()
    assert path.read_bytes() == content


def test_missing_core_starts_with_open_seed(monkeypatch):
    from fastapi.testclient import TestClient

    from mochi.admin.admin_server import app
    import mochi.config as config

    monkeypatch.setattr(config, "ADMIN_TOKEN", "test-admin-token")
    core_store.initialize_core()
    assert core_store.read_core() == core_store.DEFAULT_CORE
    assert {path.name for path in core_store.DATA_DIR.iterdir()} == {
        "core.md", ".core.lock",
    }

    client = TestClient(
        app, headers={"Authorization": "Bearer test-admin-token"},
    )
    response = client.get("/api/memory")
    assert response.status_code == 200
    assert response.json()["content"] == core_store.DEFAULT_CORE
    assert "migration" not in response.json()


def test_complete_revision_conflict_and_internal_snapshot(monkeypatch):
    core_store.replace_core("alpha\n\nbeta")
    with pytest.raises(core_store.CoreConflictError):
        core_store.replace_core_exact(
            expected_content="stale", content="changed",
        )
    assert core_store.read_core() == "alpha\n\nbeta"

    core_store.replace_core_exact(
        expected_content="alpha\n\nbeta", content="gamma\n\nbeta",
    )
    assert core_store.read_core() == "gamma\n\nbeta"
    assert list((core_store.DATA_DIR / "core_history").glob("*.md"))
    import mochi.config as config
    monkeypatch.setattr(config, "CORE_MAX_TOKENS", 10)
    with pytest.raises(core_store.CoreLimitError):
        core_store.replace_core_exact(
            expected_content="gamma\n\nbeta", content="long " * 100,
        )
    assert core_store.read_core() == "gamma\n\nbeta"
