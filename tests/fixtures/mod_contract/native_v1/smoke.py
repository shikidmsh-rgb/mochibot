import asyncio
import os
from pathlib import Path

from mochi.mod_api.v1 import SkillContext, run_candidate


skill = run_candidate(
    "local_native", Path(__file__).parent,
    Path(os.environ["MOCHI_EXTENSION_DATA_DIR"]),
    config={"LABEL": "sample", "STEP": 3, "ACCESS_TOKEN": "sample-token"},
)
assert skill.constructor_token == "sample-token"
result = asyncio.run(skill.run(SkillContext(
    trigger="script", tool_name="local_native_record", args={"text": "smoke"},
)))
assert result.success and result.state_changed, result.output
print(result.output)
