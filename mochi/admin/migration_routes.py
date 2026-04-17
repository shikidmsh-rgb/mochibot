"""Chat migration endpoints for admin portal.

Provides file upload, LLM extraction, and data import routes
for migrating chat history from ChatGPT into MochiBot.
"""

import asyncio
import logging

from fastapi import Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

log = logging.getLogger(__name__)


class ExtractRequest(BaseModel):
    session_id: str
    model_name: str
    granularity: str = "standard"


class SectionApplyRequest(BaseModel):
    section: str       # "soul" | "user_profile" | "core_memory"
    content: str = ""


class MemoriesApplyRequest(BaseModel):
    memory_items: list[dict] = []


def register_migration_routes(app, verify_token_dep):
    """Register migration endpoints on the FastAPI app."""

    from mochi.admin.migration import (
        _MAX_UPLOAD_BYTES,
        parse_chatgpt_export,
        preprocess,
        estimate_context_fit,
        start_extract_job,
        get_job_status,
        apply_section,
        start_apply_memories_job,
        MODEL_CONTEXT_WINDOWS,
    )
    from mochi.admin.admin_db import list_models

    # ── Upload & Preprocess ───────────────────────────────────────────────

    @app.post("/api/migration/upload", dependencies=[Depends(verify_token_dep)])
    async def api_migration_upload(file: UploadFile = File(...)):
        """Upload a ChatGPT export JSON and run preprocessing."""
        # Chunked read with size enforcement
        chunks = []
        total = 0
        while True:
            chunk = await file.read(1024 * 1024)  # 1MB chunks
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件超过 {_MAX_UPLOAD_BYTES // (1024*1024)}MB 限制",
                )
        raw_bytes = b"".join(chunks)

        try:
            conversations = parse_chatgpt_export(raw_bytes)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        try:
            result = preprocess(conversations)
        except Exception as e:
            log.exception("Preprocessing failed")
            return {"ok": False, "error": f"预处理失败：{e}"}

        return {
            "ok": True,
            "session_id": result.session_id,
            "conversation_count": result.conversation_count,
            "raw_message_count": result.raw_message_count,
            "filtered_message_count": result.filtered_message_count,
            "estimated_tokens": result.estimated_tokens,
        }

    # ── List Available Models ─────────────────────────────────────────────

    @app.get("/api/migration/models", dependencies=[Depends(verify_token_dep)])
    async def api_migration_models():
        """Return configured models for the user to pick from."""
        models = list_models(mask_keys=True)
        return {
            "ok": True,
            "models": models,
            "context_windows": MODEL_CONTEXT_WINDOWS,
        }

    # ── Start Extraction ──────────────────────────────────────────────────

    @app.post("/api/migration/extract", dependencies=[Depends(verify_token_dep)])
    async def api_migration_extract(req: ExtractRequest):
        """Start a background LLM extraction job."""
        try:
            job_id = start_extract_job(req.session_id, req.model_name, req.granularity)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            log.exception("Failed to start extraction job")
            return {"ok": False, "error": str(e)}
        return {"ok": True, "job_id": job_id}

    # ── Poll Job Status (extract or apply) ────────────────────────────────

    @app.get("/api/migration/job/status", dependencies=[Depends(verify_token_dep)])
    async def api_migration_job_status(job_id: str):
        """Poll for job completion (works for both extract and apply jobs)."""
        try:
            status = get_job_status(job_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return status

    # Keep old endpoint for backwards compat
    @app.get("/api/migration/extract/status", dependencies=[Depends(verify_token_dep)])
    async def api_migration_extract_status(job_id: str):
        return await api_migration_job_status(job_id)

    # ── Apply Section (soul / user / core — fast, synchronous) ────────────

    @app.post("/api/migration/apply/section", dependencies=[Depends(verify_token_dep)])
    async def api_migration_apply_section(req: SectionApplyRequest):
        """Write a single section into MochiBot."""
        try:
            result = await asyncio.to_thread(apply_section, req.section, req.content)
        except Exception as e:
            log.exception("Migration section apply failed")
            return {"ok": False, "error": str(e)}
        return result

    # ── Apply Memory Items (slow — background job with progress) ──────────

    @app.post("/api/migration/apply/memories", dependencies=[Depends(verify_token_dep)])
    async def api_migration_apply_memories(req: MemoriesApplyRequest):
        """Start background job to import memory items."""
        try:
            job_id = start_apply_memories_job(req.memory_items)
        except Exception as e:
            log.exception("Failed to start memory import job")
            return {"ok": False, "error": str(e)}
        return {"ok": True, "job_id": job_id}
