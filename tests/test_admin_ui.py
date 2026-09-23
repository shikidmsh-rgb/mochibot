from html.parser import HTMLParser
from pathlib import Path


class _ElementAttributeParser(HTMLParser):
    def __init__(self, element_id: str):
        super().__init__()
        self.element_id = element_id
        self.attributes: dict[str, str | None] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if attributes.get("id") == self.element_id:
            self.attributes = attributes


def test_model_modal_and_memory_evidence_receipts_are_safe(monkeypatch):
    index_html = (
        Path(__file__).parents[1] / "mochi" / "admin" / "index.html"
    ).read_text(encoding="utf-8")
    parser = _ElementAttributeParser("model-modal")
    parser.feed(index_html)

    assert parser.attributes is not None
    assert "onclick" not in parser.attributes

    from fastapi.testclient import TestClient

    from mochi.admin.admin_server import app
    import mochi.config as config
    from mochi.db import _connect, insert_memory_item, save_message

    monkeypatch.setattr(config, "ADMIN_TOKEN", "test-admin-token")

    source_text = "<img src=x onerror=alert(1)>" + ("x" * 2100)
    source_id = save_message(1, "user", source_text)
    other_owner_id = save_message(2, "user", "private other-owner text")
    assistant_id = save_message(1, "assistant", "assistant is not evidence")
    missing_id = assistant_id + 1000
    item_id = insert_memory_item(
        1,
        "喜欢安全可解释的记忆",
        2,
        source="lite_extracted",
        evidence_message_ids=[
            source_id, other_owner_id, assistant_id, missing_id,
        ],
    )
    legacy_id = insert_memory_item(
        1,
        "一条没有原始对话来源的旧记忆",
        1,
        source="import",
    )
    other_owner_item_id = insert_memory_item(
        2,
        "其他 owner 的记忆",
        3,
        source="lite_extracted",
        evidence_message_ids=[other_owner_id],
    )

    client = TestClient(
        app,
        headers={"Authorization": "Bearer test-admin-token"},
    )
    response = client.get(f"/api/memory-items/{item_id}/evidence")
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["source_status"] == "recorded"
    assert receipt["item"]["content"] == "喜欢安全可解释的记忆"
    assert receipt["item"]["importance"] == 2
    assert receipt["item"]["source"] == "lite_extracted"
    assert receipt["item"]["created_at"]
    assert receipt["item"]["updated_at"]
    assert receipt["source_messages"][0] == {
        "message_id": source_id,
        "available": True,
        "created_at": receipt["source_messages"][0]["created_at"],
        "content": source_text[:2000],
        "truncated": True,
    }
    assert receipt["source_messages"][1:] == [
        {"message_id": other_owner_id, "available": False},
        {"message_id": assistant_id, "available": False},
        {"message_id": missing_id, "available": False},
    ]
    assert "private other-owner text" not in response.text
    assert "assistant is not evidence" not in response.text

    legacy = client.get(f"/api/memory-items/{legacy_id}/evidence").json()
    assert legacy["source_status"] == "not_recorded"
    assert legacy["source_messages"] == []
    assert client.get(
        f"/api/memory-items/{other_owner_item_id}/evidence"
    ).status_code == 404

    listing = client.get("/api/memory-items").json()
    assert "source_messages" not in listing["items"][0]
    assert source_text not in str(listing)
    assert "esc(message.content || '')" in index_html
    assert "不代表人工编辑后的当前文字仍由它们验证" in index_html

    conn = _connect()
    stored = conn.execute(
        "SELECT evidence_message_ids FROM memory_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    conn.close()
    assert stored["evidence_message_ids"]
