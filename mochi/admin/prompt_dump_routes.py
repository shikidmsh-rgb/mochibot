"""Prompt dump debug endpoints for admin portal.

Dynamically loads and executes the dump scripts from scripts/ directory.
This keeps the dump logic separate from the admin server.
"""

import importlib.util
import logging
from pathlib import Path

from fastapi import Depends, HTTPException

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_and_run_script(script_name: str):
    """Dynamically load a dump script and return its dump() coroutine."""
    script_path = PROJECT_ROOT / "scripts" / script_name
    if not script_path.exists():
        raise HTTPException(status_code=404, detail=f"Script not found: {script_name}")

    spec = importlib.util.spec_from_file_location(
        script_name.replace(".py", ""), str(script_path)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.dump()


def register_prompt_dump_routes(app, verify_token_dep):
    """Register prompt dump endpoints on the FastAPI app."""

    @app.get("/api/prompt-dump", dependencies=[Depends(verify_token_dep)])
    async def api_prompt_dump():
        """Dump the fully-assembled chat prompt."""
        try:
            text = await _load_and_run_script("dump_prompt.py")
        except HTTPException:
            raise
        except Exception as e:
            log.exception("prompt-dump failed")
            raise HTTPException(status_code=500, detail=str(e))
        return {"text": text}

    @app.get("/api/think-prompt-dump", dependencies=[Depends(verify_token_dep)])
    async def api_think_prompt_dump():
        """Dump the fully-assembled Think prompt."""
        try:
            text = await _load_and_run_script("dump_think_prompt.py")
        except HTTPException:
            raise
        except Exception as e:
            log.exception("think-prompt-dump failed")
            raise HTTPException(status_code=500, detail=str(e))
        return {"text": text}
