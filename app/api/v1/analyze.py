"""
app/api/v1/analyze.py
─────────────────────
FastAPI router for the SEO Trust Path Analysis endpoints.

Routes
------
POST   /api/v1/analyze                  — Submit analysis job, get task_id
GET    /api/v1/task/{task_id}           — Poll task status / result
GET    /api/v1/task/{task_id}/stream    — SSE stream of task progress
DELETE /api/v1/task/{task_id}           — Cancel / delete a task
GET    /api/v1/health                   — Health check
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.task_store import delete_state, get_state, set_state, subscribe
from app.models.request import AnalyzeRequest
from app.models.response import (
    ErrorResponse,
    HealthResponse,
    ProgressInfo,
    TaskCreateResponse,
    TaskStatus,
    TaskStatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["SEO Analysis"])

# ─────────────────────────────────────────────────────────────────────────────
# Active task counter — tracks how many pipelines are currently running.
# Using a plain int counter (incremented/decremented atomically within the
# single asyncio event loop) is simpler and race-free compared to checking
# semaphore internals. MAX_CONCURRENT_REQUESTS controls the cap.
# ─────────────────────────────────────────────────────────────────────────────
_active_pipeline_count: int = 0

# Tracks running asyncio Tasks by task_id so DELETE can attempt cancellation
_running_tasks: dict[str, asyncio.Task] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_initial_state(task_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "task_id": task_id,
        "status": TaskStatus.QUEUED.value,
        "progress": {
            "stage": "queued",
            "percent": 0,
            "message": "排队中，即将开始…",
        },
        "result": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/v1/analyze
# ─────────────────────────────────────────────────────────────────────────────
@router.post(
    "/analyze",
    response_model=TaskCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit an SEO trust-path analysis job",
    responses={
        202: {"description": "Task accepted, connect to /task/{task_id}/stream for results"},
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)
async def submit_analysis(
    request: Request,
    body: AnalyzeRequest,
) -> TaskCreateResponse:
    """
    Accept an analysis request and start the pipeline as a background
    asyncio task. Returns a task_id for streaming progress.
    """
    from urllib.parse import urlparse as _urlparse

    global _active_pipeline_count

    url_str = str(body.url)

    # ── Generate task and store initial state ─────────────────────────────────
    task_id = str(uuid.uuid4())
    initial_state = _build_initial_state(task_id)
    created_at: str = initial_state["created_at"]
    set_state(task_id, initial_state)

    # ── Resolve gbp_url ───────────────────────────────────────────────────────
    if body.gbp_url:
        gbp_url = body.gbp_url
    else:
        parsed = _urlparse(url_str)
        gbp_url = f"{parsed.scheme}://{parsed.netloc}"

    # ── Launch pipeline as background asyncio task ────────────────────────────
    from app.tasks.pipeline import run_pipeline  # noqa: PLC0415

    # Reject immediately if all slots are occupied (atomic check in single event loop)
    if _active_pipeline_count >= settings.MAX_CONCURRENT_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Server is busy — too many concurrent analyses. Please retry in a moment.",
        )

    async def _guarded_pipeline() -> None:
        global _active_pipeline_count
        _active_pipeline_count += 1
        try:
            await run_pipeline(
                task_id=task_id,
                url=url_str,
                page_type=body.page_type.value,
                language=body.language.value,
                gbp_url=gbp_url,
                created_at=created_at,
            )
        finally:
            _active_pipeline_count -= 1
            _running_tasks.pop(task_id, None)
            # Auto-cleanup: remove task state 10 minutes after completion
            # so clients have time to fetch the result before it's gone.
            await asyncio.sleep(600)
            # Only delete if the task still exists and is terminal
            # (guards against manual deletion + re-creation edge cases)
            state = get_state(task_id)
            if state and state.get("status") in ("done", "failed"):
                delete_state(task_id)

    bg_task = asyncio.create_task(_guarded_pipeline(), name=f"pipeline-{task_id}")
    _running_tasks[task_id] = bg_task

    logger.info(
        "Task started task_id=%s url=%s page_type=%s language=%s",
        task_id, url_str, body.page_type.value, body.language.value,
    )

    return TaskCreateResponse(
        task_id=task_id,
        status=TaskStatus.QUEUED,
        estimated_seconds=60,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/task/{task_id}
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/task/{task_id}",
    response_model=TaskStatusResponse,
    summary="Poll task status and retrieve results",
    responses={
        200: {"description": "Task snapshot"},
        404: {"model": ErrorResponse, "description": "Task not found"},
    },
)
async def get_task_status(
    request: Request,
    task_id: str,
) -> TaskStatusResponse:
    """Return the current state of a task (fallback polling endpoint)."""
    state = get_state(task_id)

    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found — it may have expired or never existed.",
        )

    created_at = datetime.fromisoformat(state["created_at"])
    updated_at = datetime.fromisoformat(state["updated_at"])

    return TaskStatusResponse(
        task_id=state["task_id"],
        status=TaskStatus(state["status"]),
        progress=ProgressInfo(**state["progress"]),
        result=state.get("result"),
        error=state.get("error"),
        created_at=created_at,
        updated_at=updated_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/task/{task_id}/stream  — SSE
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/task/{task_id}/stream",
    summary="Stream task progress via Server-Sent Events",
    responses={
        200: {"description": "SSE stream — each event is a task state snapshot"},
        404: {"model": ErrorResponse, "description": "Task not found"},
    },
)
async def stream_task_status(
    request: Request,
    task_id: str,
) -> StreamingResponse:
    """
    Subscribe to in-memory task events and forward each state update
    to the client as an SSE event. No Redis required.
    """
    if get_state(task_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found — it may have expired or never existed.",
        )

    async def _event_generator():
        async for state in subscribe(task_id, timeout=300.0):
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(state)}\n\n"
        # Send a final comment to signal stream end (helps some clients)
        yield ": stream closed\n\n"

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /api/v1/task/{task_id}
# ─────────────────────────────────────────────────────────────────────────────
@router.delete(
    "/task/{task_id}",
    status_code=status.HTTP_200_OK,
    summary="Cancel or delete a task",
    responses={
        200: {"description": "Task deleted"},
        404: {"model": ErrorResponse, "description": "Task not found"},
    },
)
async def delete_task(
    request: Request,
    task_id: str,
) -> None:
    """Cancel and remove a task from the in-memory store."""
    if get_state(task_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    delete_state(task_id)

    # Cancel the background asyncio task if still running
    bg_task = _running_tasks.pop(task_id, None)
    if bg_task and not bg_task.done():
        bg_task.cancel()

    logger.info("Task deleted task_id=%s", task_id)


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/v1/health
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
    tags=["Health"],
)
async def health_check() -> HealthResponse:
    """Return service liveness status."""
    return HealthResponse(
        status="ok",
        version=settings.APP_VERSION,
    )
