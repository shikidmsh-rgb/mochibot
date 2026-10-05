import pytest

from mochi import db
from mochi.admin import admin_crypto
from mochi.skills.base import SkillContext
from mochi.skills.skill_management.handler import SkillManagementSkill
from mochi.tool_execution import model_result_for, serialized_arguments


@pytest.mark.asyncio
async def test_secret_settings_require_authority_and_never_fall_back_to_plaintext(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "isolated-test-encryption-root")
    monkeypatch.setattr(admin_crypto, "_fernet_instance", None)
    secret = "isolated-test-secret"
    args = {"action": "set", "id": "skills.web_search.config.BAIDU_API_KEY", "value": secret}
    context = SkillContext(
        trigger="tool_call", actor="main", user_id=1, source="chat",
        owner_authorized=False, tool_name="manage_settings", args=args,
    )
    skill = SkillManagementSkill()
    assert not (await skill.execute(context)).success
    assert db.get_skill_config("web_search") == {}
    context.owner_authorized = True
    saved = await skill.execute(context)
    assert saved.success and saved.state_changed
    ciphertext = db.get_skill_config("web_search")["BAIDU_API_KEY"]
    assert ciphertext != secret and admin_crypto.decrypt_api_key(ciphertext) == secret
    assert secret not in model_result_for(saved)
    assert secret not in serialized_arguments("manage_settings", args)
    monkeypatch.setattr(admin_crypto, "encrypt_api_key", lambda value: value)
    context.args = {**args, "value": "replacement-secret"}
    rejected = await skill.execute(context)
    assert not rejected.success and rejected.error_code == "secret_encryption_failed"
    assert db.get_skill_config("web_search")["BAIDU_API_KEY"] == ciphertext
