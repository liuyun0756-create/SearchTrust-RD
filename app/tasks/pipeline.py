"""
app/tasks/pipeline.py
─────────────────────
Main Celery task: SEO trust-path analysis pipeline.

Stages
------
Stage 1 — Scraping   (  0 – 30 %)   Jina Reader + SerpAPI GBP
Stage 2 — Analysing  ( 30 – 90 %)   Dify streaming workflow
Stage 3 — Done       ( 90 – 100%)   Persist report + update status

Every stage updates the task state in Redis so the FastAPI polling
endpoint always has a fresh snapshot to return.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from celery import Task
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded

from app.core.config import settings
from app.core.redis_client import RedisClient, get_redis, reset_pool
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Redis key helpers
# ─────────────────────────────────────────────────────────────────────────────

def _task_key(task_id: str) -> str:
    return f"task:status:{task_id}"


def _report_cache_key(url: str, page_type: str, language: str) -> str:
    raw = f"{url}:{page_type}:{language}"
    digest = hashlib.md5(raw.encode()).hexdigest()
    return f"report:cache:{digest}"


# ─────────────────────────────────────────────────────────────────────────────
# State updater
# ─────────────────────────────────────────────────────────────────────────────

def _sync_update_state(
    redis: RedisClient,
    task_id: str,
    status: str,
    stage: str,
    percent: int,
    message: str,
    result: Any = None,
    error: str | None = None,
) -> None:
    """
    Synchronously update the task state in Redis.

    We use the sync ``asyncio.run`` approach because Celery workers are
    *not* running in an async event loop — the pipeline task itself is
    a regular synchronous Celery task that spawns its own event loop via
    ``asyncio.run()``.  We cannot call async redis methods directly, so
    we schedule them on the dedicated loop passed from the task.
    """
    # This function is always called from within asyncio.run() context,
    # so we use a coroutine wrapper and schedule it immediately.
    raise NotImplementedError("Use _async_update_state inside the async pipeline")


async def _async_update_state(
    redis: RedisClient,
    task_id: str,
    status: str,
    stage: str,
    percent: int,
    message: str,
    result: Any = None,
    error: str | None = None,
) -> None:
    """
    Persist the current task state to Redis.

    Parameters
    ----------
    redis:      RedisClient instance (reused across the pipeline run)
    task_id:    Task UUID
    status:     Lifecycle status string (queued/scraping/…/done/failed)
    stage:      Current stage label for the progress snapshot
    percent:    0-100 completion percentage
    message:    Human-readable status message
    result:     Final report dict (set only when done)
    error:      Error description (set only when failed)
    """
    now = datetime.now(timezone.utc).isoformat()

    # Read existing state so we preserve ``created_at``
    existing = await redis.get_json(_task_key(task_id)) or {}
    created_at = existing.get("created_at", now)

    state: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "progress": {
            "stage": stage,
            "percent": percent,
            "message": message,
        },
        "result": result,
        "error": error,
        "created_at": created_at,
        "updated_at": now,
    }

    # Use a shorter TTL for terminal states (done/failed) — the client
    # has already polled the result; no need to keep it for 24 h.
    # In-progress states use TASK_RESULT_TTL (30 min) as a safety window.
    terminal = status in ("done", "failed")
    ttl = settings.TASK_DONE_TTL if terminal else settings.TASK_RESULT_TTL

    ok = await redis.set_json(_task_key(task_id), state, ttl=ttl)
    if not ok:
        logger.error("Failed to persist task state to Redis — task_id=%s", task_id)
    else:
        logger.debug(
            "Task state updated — task_id=%s status=%s stage=%s percent=%d",
            task_id,
            status,
            stage,
            percent,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Async pipeline implementation
# ─────────────────────────────────────────────────────────────────────────────

async def _run_pipeline(
    task_id: str,
    url: str,
    page_type: str,
    language: str,
    gbp_url: str,
) -> dict[str, Any]:
    """
    Async implementation of the full analysis pipeline.

    Separated from the Celery task wrapper so it can be awaited cleanly
    inside ``asyncio.run()``.

    Returns
    -------
    dict — the final analysis report.
    """
    redis = get_redis()

    # ── Stage 1: Scraping (0 → 30 %) ─────────────────────────────────────────
    await _async_update_state(
        redis, task_id,
        status="scraping",
        stage="scraping",
        percent=5,
        message="开始抓取页面内容…",
    )

    from app.tasks.scraper import scrape  # noqa: PLC0415

    try:
        logger.info("Pipeline stage=scraping task_id=%s url=%s gbp_url=%s", task_id, url, gbp_url)
        scrape_result = await scrape(url, gbp_url=gbp_url)
    except RuntimeError as exc:
        logger.error("Scraping failed task_id=%s: %s", task_id, exc)
        await _async_update_state(
            redis, task_id,
            status="failed",
            stage="scraping",
            percent=10,
            message="页面抓取失败",
            error=str(exc),
        )
        raise

    content: str = scrape_result.get("content", "")
    gbp_data: dict[str, Any] = scrape_result.get("gbp", {})

    await _async_update_state(
        redis, task_id,
        status="analyzing",
        stage="scraping",
        percent=30,
        message=f"页面抓取完成（{len(content)} 字符），准备分析…",
    )

    # ── Stage 2: Dify workflow (30 → 90 %) ───────────────────────────────────
    logger.info("Pipeline stage=analyzing task_id=%s", task_id)

    from app.tasks.dify_client import call_dify_workflow  # noqa: PLC0415

    async def _progress_cb(stage: str, percent: int, message: str) -> None:
        """Relay Dify SSE progress to Redis."""
        await _async_update_state(
            redis, task_id,
            status="analyzing" if percent < 85 else "reporting",
            stage=stage,
            percent=percent,
            message=message,
        )

    try:
        report = await call_dify_workflow(
            url=url,
            page_type=page_type,
            language=language,
            content=content,
            gbp_data=gbp_data,
            task_id=task_id,
            progress_callback=_progress_cb,
        )
    except RuntimeError as exc:
        logger.error("Dify workflow failed task_id=%s: %s", task_id, exc)
        await _async_update_state(
            redis, task_id,
            status="failed",
            stage="analyzing",
            percent=50,
            message="AI 分析失败",
            error=str(exc),
        )
        raise

    # ── Stage 3: Done (90 → 100 %) ───────────────────────────────────────────
    logger.info("Pipeline stage=done task_id=%s", task_id)

    # Normalise the report: Dify may return ``score`` as a JSON string
    final_report: dict[str, Any]
    if isinstance(report, dict):
        score_raw = report.get("score")
        if isinstance(score_raw, str):
            try:
                report["score"] = json.loads(score_raw)
            except json.JSONDecodeError:
                pass   # keep as string
        final_report = report
    else:
        final_report = {"raw": report}

    # Persist report-level cache（附带生成时间，供缓存命中时复原时间戳）
    now = datetime.now(timezone.utc).isoformat()
    cached_payload = {"report": final_report, "generated_at": now}
    report_cache_key = _report_cache_key(url, page_type, language)
    await redis.set_json(report_cache_key, cached_payload, ttl=settings.REPORT_CACHE_TTL)

    await _async_update_state(
        redis, task_id,
        status="done",
        stage="done",
        percent=100,
        message="分析报告已生成",
        result=final_report,
    )

    logger.info("Pipeline complete task_id=%s", task_id)
    return final_report


# ─────────────────────────────────────────────────────────────────────────────
# Celery task
# ─────────────────────────────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="seo_analysis.analyze_pipeline",
    max_retries=2,
    default_retry_delay=30,
    queue="seo_analysis",
    acks_late=True,
)
def analyze_pipeline(
    self: Task,
    task_id: str,
    url: str,
    page_type: str,
    language: str,
    gbp_url: str = "",
) -> dict[str, Any]:
    """
    Celery entry-point for the SEO trust-path analysis pipeline.

    Parameters
    ----------
    task_id:    UUID generated by the API on task submission.
    url:        Target page URL (str, not HttpUrl).
    page_type:  Page classification string.
    language:   Report language string.
    gbp_url:    Website URL for GBP domain lookup.

    Returns
    -------
    dict — final analysis report (also stored in Redis).
    """
    logger.info(
        "Celery task started — task_id=%s url=%s page_type=%s language=%s gbp_url=%s",
        task_id,
        url,
        page_type,
        language,
        gbp_url,
    )

    try:
        # Reset the Redis connection pool before creating a new event loop.
        # Celery workers call asyncio.run() which creates a fresh loop each
        # time; any pool bound to a previous loop would cause
        # "Future attached to a different loop" errors.
        reset_pool()
        result = asyncio.run(
            _run_pipeline(
                task_id=task_id,
                url=url,
                page_type=page_type,
                language=language,
                gbp_url=gbp_url or url,
            )
        )
        return result

    except SoftTimeLimitExceeded:
        # Celery's soft time limit was hit (3 min)
        logger.error("Task soft time-limit exceeded — task_id=%s", task_id)
        # Best-effort state update (may fail if Redis is slow)
        try:
            reset_pool()
            asyncio.run(
                _async_update_state(
                    get_redis(), task_id,
                    status="failed",
                    stage="timeout",
                    percent=0,
                    message="任务超时（超过3分钟）",
                    error="SoftTimeLimitExceeded",
                )
            )
        except Exception:  # noqa: BLE001
            pass
        raise

    except Exception as exc:
        logger.error(
            "Pipeline unhandled exception — task_id=%s: %s",
            task_id,
            exc,
            exc_info=True,
        )
        # Retry if we haven't exhausted attempts
        try:
            raise self.retry(exc=exc, countdown=2 ** self.request.retries * 10)
        except MaxRetriesExceededError:
            logger.error(
                "Max retries exceeded — task_id=%s marking as failed",
                task_id,
            )
            try:
                reset_pool()
                asyncio.run(
                    _async_update_state(
                        get_redis(), task_id,
                        status="failed",
                        stage="unknown",
                        percent=0,
                        message="任务失败（已达最大重试次数）",
                        error=str(exc),
                    )
                )
            except Exception:  # noqa: BLE001
                pass
            raise
