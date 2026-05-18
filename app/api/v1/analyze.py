"""
app/api/v1/analyze.py
─────────────────────
FastAPI router for the SEO Trust Path Analysis endpoints.

Routes
------
POST   /api/v1/analyze            — Submit analysis job, get task_id
GET    /api/v1/task/{task_id}     — Poll task status / result
DELETE /api/v1/task/{task_id}     — Cancel / delete a task
GET    /api/v1/health             — Health check
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.core.config import settings
from app.core.redis_client import get_redis
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

# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/v1", tags=["SEO Analysis"])


# ─────────────────────────────────────────────────────────────────────────────
# Helper: task Redis key
# ─────────────────────────────────────────────────────────────────────────────
def _task_key(task_id: str) -> str:
    return f"task:status:{task_id}"


def _report_cache_key(url: str, page_type: str, language: str) -> str:
    raw = f"{url}:{page_type}:{language}"
    digest = hashlib.md5(raw.encode()).hexdigest()
    return f"report:cache:{digest}"


def _build_initial_state(task_id: str) -> dict[str, Any]:
    """Build the initial task state dict stored in Redis."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "task_id": task_id,
        "status": TaskStatus.QUEUED.value,
        "progress": {
            "stage": "queued",
            "percent": 0,
            "message": "任务已接收，等待处理…",
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
        202: {"description": "Task accepted, poll /task/{task_id} for results"},
        422: {"model": ErrorResponse, "description": "Validation error"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
)
async def submit_analysis(
    request: Request,
    body: AnalyzeRequest,
) -> TaskCreateResponse:
    """
    Accept an analysis request, check the report cache, enqueue a Celery
    task if no cache hit, and return a ``task_id`` for polling.
    """
    redis = get_redis()
    url_str = str(body.url)

    # ── 1. Check report-level cache ───────────────────────────────────────────
    cache_key = _report_cache_key(url_str, body.page_type.value, body.language.value)
    cached = await redis.get_json(cache_key)
    if cached is not None:
        logger.info("Report cache hit for url=%s page_type=%s", url_str, body.page_type)

        final_report = cached.get("report", cached)   # 兼容旧格式（无 report 包装）

        # Return the report directly in the POST response — no Redis write,
        # no polling round-trip needed.  Client detects cache hit via
        # status='done' + result != None.
        return TaskCreateResponse(
            task_id=str(uuid.uuid4()),
            status=TaskStatus.DONE,
            estimated_seconds=0,
            result=final_report,
        )

    # ── 2. Generate task ID and persist initial state ─────────────────────────
    task_id = str(uuid.uuid4())
    initial_state = _build_initial_state(task_id)
    await redis.set_json(_task_key(task_id), initial_state, ttl=settings.TASK_RESULT_TTL)

    # ── 3. Enqueue Celery task ────────────────────────────────────────────────
    # Import here to avoid circular import at module load time
    from app.tasks.pipeline import analyze_pipeline  # noqa: PLC0415

    # gbp_url 未提供时默认用主域名
    from urllib.parse import urlparse as _urlparse
    if body.gbp_url:
        gbp_url = body.gbp_url
    else:
        parsed = _urlparse(url_str)
        gbp_url = f"{parsed.scheme}://{parsed.netloc}"

    try:
        analyze_pipeline.apply_async(
            args=[task_id, url_str, body.page_type.value, body.language.value, gbp_url],
            task_id=task_id,
            queue="seo_analysis",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to enqueue task task_id=%s: %s",
            task_id,
            exc,
            exc_info=True,
        )
        # Clean up the orphaned task state from Redis
        await redis.delete(_task_key(task_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task queue unavailable — Celery broker connection failed. "
                   "Ensure the Celery worker is running.",
        )

    logger.info(
        "Task enqueued task_id=%s url=%s page_type=%s language=%s gbp_url=%s",
        task_id,
        url_str,
        body.page_type.value,
        body.language.value,
        gbp_url,
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
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
)
async def get_task_status(
    request: Request,
    task_id: str,
) -> TaskStatusResponse:
    """
    Return the current state of a task identified by ``task_id``.

    The ``result`` field is populated once ``status == 'done'``.
    The ``error`` field is populated when ``status == 'failed'``.
    """
    redis = get_redis()
    state = await redis.get_json(_task_key(task_id))

    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found — it may have expired or never existed.",
        )

    # Coerce ISO strings to datetime objects
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
    """
    Remove a task from Redis.

    If the task is still running in Celery it will finish but its result
    will no longer be visible via the polling endpoint.
    """
    redis = get_redis()
    key = _task_key(task_id)
    state = await redis.get_json(key)

    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    # Attempt to revoke Celery task (best-effort; may already be done)
    try:
        from app.tasks.celery_app import celery_app  # noqa: PLC0415
        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
        logger.info("Celery task revoked task_id=%s", task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to revoke Celery task %s: %s", task_id, exc)

    await redis.delete(key)
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
    """
    Return service liveness status.

    Checks Redis connectivity; Celery worker health is not probed here
    to keep the response fast.
    """
    redis = get_redis()
    redis_ok = await redis.ping()
    return HealthResponse(
        status="ok" if redis_ok else "degraded",
        version=settings.APP_VERSION,
        redis=redis_ok,
    )
