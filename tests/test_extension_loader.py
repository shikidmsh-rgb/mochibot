"""Loading stays import-light and accepts only one owned ordinary Skill."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

from mochi.extensions import loader, store
from mochi.skills.base import SkillContext
from tests.test_extension_store import HANDLER, metadata


@pytest.fixture(autouse=True)
def fresh_db():
    """No database setup is needed for loading personal Python packages."""


@pytest.fixture(autouse=True)
def mock_config():
    """No live configuration is needed for loading personal Python packages."""


@pytest.fixture
def package(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path / "extensions")
    package = tmp_path / "code"
    package.mkdir()
    for name, content in {"__init__.py": "", "handler.py": HANDLER, "SKILL.md": metadata()}.items():
        (package / name).write_text(content, encoding="utf-8")
    return package


def load(package, **kwargs):
    return loader.load_from_path("local_example", package, data_dir=package.parent / "owned_data", **kwargs)


def test_relative_imports_config_metadata_and_real_run(package):
    (package / "helper.py").write_text("VALUE = 'relative'", encoding="utf-8")
    (package / "handler.py").write_text(
        HANDLER.replace(
            "from mochi.skills.base import Skill, SkillResult",
            "from mochi.skills.base import Skill, SkillResult\nfrom .helper import VALUE",
        ).replace(
            'return SkillResult(output=context.args.get("value", ""))',
            'self.data_dir.joinpath("saved.txt").write_text(VALUE, encoding="utf-8")\n'
            '        return SkillResult(output=VALUE + self.config["suffix"])',
        ),
        encoding="utf-8",
    )
    config = {"suffix": "-configured"}
    skill = load(package, config=config)
    config["suffix"] = "-mutated"
    assert skill.name == "local_example"
    assert skill.external
    assert Path(skill.__module_file__) == package / "handler.py"
    assert skill.data_dir == package.parent / "owned_data"
    result = asyncio.run(skill.run(SkillContext(trigger="tool_call", tool_name="local_example_echo")))
    assert result.success and result.execution_started
    assert result.output == "relative-configured"
    assert (skill.data_dir / "saved.txt").read_text(encoding="utf-8") == "relative"


def test_metadata_path_and_fixture_available_during_constructor(package, monkeypatch):
    import mochi.db as db

    monkeypatch.setattr(db, "get_skill_config", lambda name: {"ACCESS_TOKEN": "LIVE_DB_SENTINEL"})
    (package / "handler.py").write_text(
        HANDLER.replace(
            "    async def execute", "    def __init__(self):\n"
            "        super().__init__()\n"
            "        self.constructor_name = self.skill_md['meta']['name']\n"
            "        self.constructor_token = self.get_config('ACCESS_TOKEN')\n"
            "        self.constructor_data_exists = self.data_dir.is_dir()\n\n"
            "    async def execute",
        ), encoding="utf-8",
    )
    skill = load(package, config={"ACCESS_TOKEN": "TEST_FIXTURE"})
    assert skill.constructor_name == "local_example"
    assert skill.constructor_token == "TEST_FIXTURE"
    assert skill.constructor_data_exists
    assert load(package).constructor_token == ""


def test_distinct_package_paths_get_independent_helpers(package):
    (package / "helper.py").write_text("VALUE = 'one'", encoding="utf-8")
    (package / "handler.py").write_text(
        HANDLER.replace("class Example", "from .helper import VALUE\n\nclass Example").replace(
            'context.args.get("value", "")', "VALUE",
        ), encoding="utf-8",
    )
    other = package.parent / "another_copy"
    store.copy_package(package, other)
    (other / "helper.py").write_text("VALUE = 'two'", encoding="utf-8")
    one = load(package)
    two = load(other)
    assert type(one).__module__ != type(two).__module__
    assert asyncio.run(one.run(SkillContext(trigger="tool_call"))).output == "one"
    assert asyncio.run(two.run(SkillContext(trigger="tool_call"))).output == "two"


@pytest.mark.parametrize("change,code", [
    (lambda text: text.replace("name: local_example", "name: local_other"), "invalid_metadata"),
    (lambda text: text.replace("type: tool", "type: hybrid"), "unsupported_hooks"),
    (lambda text: text.replace("type: tool", "type: invalid"), "unsupported_hooks"),
    (lambda text: text.replace("type: tool", "type: tool\ntriggers: [heartbeat]"), "unsupported_hooks"),
    (lambda text: text.replace("type: tool", "type: tool\nlocked: true"), "unsupported_hooks"),
    (lambda text: text.replace("type: tool", "type: tool\nsense:\n  observer: Example"), "unsupported_hooks"),
    (lambda text: text.replace("type: tool", "type: tool\ndiary: [example]"), "unsupported_hooks"),
    (lambda text: text.replace("local_example_echo", "builtin_echo"), "invalid_tool_name"),
    (lambda text: text.replace("local_example_echo", "local_example_" + "x" * 55), "invalid_tool_name"),
    (lambda text: text.replace("(on_demand)", "(always)"), "invalid_metadata"),
    (lambda text: text.replace("(on_demand)", ""), "invalid_metadata"),
    (lambda text: text.replace("| string |", "| object |"), "invalid_schema"),
    (lambda text: text.replace("| string |", "| array (items: object) |"), "invalid_schema"),
    (lambda text: text.replace("| string |", "| string (enum: a,b) garbage |"), "invalid_schema"),
    (lambda text: text + "| value | string | yes | duplicate |\n", "invalid_schema"),
])
def test_invalid_metadata_and_schema_rejected_before_import(package, change, code):
    (package / "handler.py").write_text("raise RuntimeError('code executed')", encoding="utf-8")
    (package / "SKILL.md").write_text(change(metadata()), encoding="utf-8")
    with pytest.raises(store.ExtensionError) as caught:
        load(package)
    assert caught.value.code == code


def test_metadata_scalar_and_string_array_formats(package):
    text = metadata().replace("| value | string | yes | Value to echo. |", """| value | string (enum: one, two) | yes | Value. |
| count | integer | no | Count. |
| ratio | number | no | Ratio. |
| enabled | boolean | no | Enabled. |
| paths | array (items: string) | no | Paths. |
| labels | array | no | Labels. |""")
    (package / "SKILL.md").write_text(text, encoding="utf-8")
    properties = load(package).get_tools()[0]["function"]["parameters"]["properties"]
    assert properties["value"]["enum"] == ["one", "two"]
    assert properties["paths"]["items"] == {"type": "string"}
    assert properties["labels"]["items"] == {"type": "string"}


@pytest.mark.parametrize("method", ["init_schema", "diary_status", "run", "get_tools", "available_tools", "prompt_section", "observe"])
def test_hooks_are_rejected_and_failed_namespace_is_cleaned(package, method):
    (package / "handler.py").write_text(
        HANDLER + f"\n    def {method}(self, *args):\n        return None\n", encoding="utf-8",
    )
    before = {name for name in sys.modules if name.startswith("_mochi_extension_")}
    with pytest.raises(store.ExtensionError) as caught:
        load(package)
    assert caught.value.code == "unsupported_hooks"
    assert {name for name in sys.modules if name.startswith("_mochi_extension_")} == before


def test_imported_subclass_is_not_handler_owned_and_multiple_classes_rejected(package):
    (package / "helper.py").write_text(HANDLER, encoding="utf-8")
    (package / "handler.py").write_text("from .helper import Example", encoding="utf-8")
    with pytest.raises(store.ExtensionError, match="exactly one"):
        load(package)
    (package / "handler.py").write_text(HANDLER + "\nclass Second(Example):\n    pass\n", encoding="utf-8")
    with pytest.raises(store.ExtensionError, match="exactly one"):
        load(package)
    (package / "handler.py").write_text(HANDLER + "\nAlias = Example\n", encoding="utf-8")
    assert load(package).name == "local_example"


def test_import_failure_cleans_package_and_helper_namespace(package):
    (package / "helper.py").write_text("VALUE = 1", encoding="utf-8")
    (package / "handler.py").write_text("from .helper import VALUE\nraise RuntimeError('broken import')", encoding="utf-8")
    before = {name for name in sys.modules if name.startswith("_mochi_extension_")}
    with pytest.raises(RuntimeError, match="broken import"):
        load(package)
    assert {name for name in sys.modules if name.startswith("_mochi_extension_")} == before


def test_validation_never_executes_code_and_rejects_compile_errors(package):
    (package / "__init__.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")
    assert loader.validate_package("local_example", package)["meta"]["name"] == "local_example"
    (package / "helper.py").write_text("return 1", encoding="utf-8")
    with pytest.raises(store.ExtensionError) as caught:
        loader.validate_package("local_example", package)
    assert caught.value.code == "invalid_python"
    assert loader.read_metadata("local_example", package)["meta"]["name"] == "local_example"


def test_collisions_are_rejected(package, monkeypatch):
    import mochi.skills as registry

    monkeypatch.setattr(registry, "_tool_map", {"local_example_echo": "builtin"})
    with pytest.raises(store.ExtensionError) as caught:
        load(package)
    assert caught.value.code == "tool_collision"


def test_importing_extensions_does_not_load_runtime_services():
    code = """import json, sys
from mochi.extensions import store, loader
print(json.dumps(sorted(name for name in sys.modules if name in {
    'mochi.db', 'mochi.config', 'mochi.runtime', 'mochi.diary',
    'mochi.transports', 'mochi.skill_config_resolver'
})))
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == []
