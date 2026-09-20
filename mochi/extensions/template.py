"""Neutral, editable example files for a trusted personal Python tool."""

from textwrap import dedent


def files(extension_id: str) -> dict[str, str]:
    from mochi.extensions.store import extension_root

    extension_root(extension_id)
    tool_name = f"{extension_id}_echo"
    return {
        "__init__.py": "",
        "SKILL.md": dedent(f"""\
            ---
            name: {extension_id}
            mod_api: 1
            description: "Personal echo example; returns the supplied text."
            type: tool
            ---

            ## Tools

            ### {tool_name} (on_demand)
            Return the supplied text without storing it or calling a service.

            | Parameter | Type | Required | Description |
            |-----------|------|----------|-------------|
            | text | string | yes | Text to return |

            ## Capability Context

            - This example only echoes text. It does not implement another feature.
            - Personal Python runs with the owner's local permissions, not in a
              security sandbox. Code can access host files, configuration and networks.
            """),
        "handler.py": dedent(f"""\
            from mochi.mod_api.v1 import Skill, SkillContext, SkillResult


            class PersonalSkill(Skill):
                async def execute(self, context: SkillContext) -> SkillResult:
                    if context.tool_name != "{tool_name}":
                        return SkillResult(output="Unknown tool", success=False)
                    text = context.args.get("text")
                    if not isinstance(text, str):
                        return SkillResult(output="text must be a string", success=False)
                    return SkillResult(output=text)
            """),
        "smoke.py": dedent(f"""\
            \"\"\"Edit these assertions alongside your handler; no live Mochi runtime.

            run_extension supplies disposable package/data paths. For direct manual
            execution this script creates a disposable data directory beside itself.
            Neither mode is a security sandbox or can undo external side effects.
            \"\"\"

            import asyncio
            import os
            from pathlib import Path
            from tempfile import TemporaryDirectory

            from mochi.mod_api.v1 import SkillContext, run_candidate


            async def smoke(data_dir: Path) -> None:
                skill = run_candidate(
                    "{extension_id}", Path(__file__).parent, data_dir,
                    config={{}},  # Override schema defaults with explicit test values here.
                )
                result = await skill.run(SkillContext(
                    trigger="script",
                    tool_name="{tool_name}",
                    args={{"text": "Hello from {extension_id}"}},
                ))
                print(result.output, flush=True)
                assert result.success, result.output
                assert result.output == "Hello from {extension_id}", result.output


            if __name__ == "__main__":
                supplied_data = os.environ.get("MOCHI_EXTENSION_DATA_DIR")
                if supplied_data:
                    asyncio.run(smoke(Path(supplied_data)))
                else:
                    with TemporaryDirectory(prefix=".smoke-", dir=Path(__file__).parent) as data:
                        asyncio.run(smoke(Path(data)))
            """),
    }
