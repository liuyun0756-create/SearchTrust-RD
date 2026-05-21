"""
app/tasks/pipeline.py
─────────────────────
SEO trust-path analysis pipeline.

Stages
------
Stage 1 — Scraping   (  0 – 30 %)   Jina Reader + SerpAPI GBP
Stage 2 — Analysing  ( 30 – 90 %)   Dify streaming workflow
Stage 3 — Done       ( 90 – 100%)   Persist report + update status

No longer a Celery task — runs directly as an asyncio coroutine via
asyncio.create_task() from the FastAPI request handler.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.task_store import set_state

logger = logging.getLogger(__name__)
_PROGRESS_THROTTLE_PCT: int = 10


# ─────────────────────────────────────────────────────────────────────────────
# State updater
# ─────────────────────────────────────────────────────────────────────────────

def _update_state(
    task_id: str,
    created_at: str,
    status: str,
    stage: str,
    percent: int,
    message: str,
    result: Any = None,
    error: str | None = None,
) -> None:
    """Persist the current task state to the in-memory store."""
    now = datetime.now(timezone.utc).isoformat()
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
    set_state(task_id, state)
    logger.debug(
        "Task state updated — task_id=%s status=%s stage=%s percent=%d",
        task_id, status, stage, percent,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline implementation
# ─────────────────────────────────────────────────────────────────────────────

async def run_pipeline(
    task_id: str,
    url: str,
    page_type: str,
    language: str,
    gbp_url: str,
    created_at: str,
) -> dict[str, Any]:
    """
    Full SEO analysis pipeline as a plain async function.

    Called via asyncio.create_task() from the FastAPI submit endpoint.
    All progress updates go directly to the in-memory task store.
    """

    try:
        return await _run_pipeline_inner(
            task_id=task_id,
            url=url,
            page_type=page_type,
            language=language,
            gbp_url=gbp_url,
            created_at=created_at,
        )
    except asyncio.CancelledError:
        # Task was explicitly cancelled via DELETE /task/{id} — not an error,
        # just stop silently without overwriting the already-deleted state.
        logger.info("Pipeline cancelled task_id=%s", task_id)
        raise  # re-raise so asyncio knows the task is properly cancelled
    except Exception as exc:  # noqa: BLE001
        # Catch-all: any unexpected exception that wasn't handled inside
        # the pipeline leaves the task in a terminal failed state instead
        # of hanging forever in scraping/analyzing.
        logger.error(
            "Pipeline unexpected error task_id=%s: %s",
            task_id, exc, exc_info=True,
        )
        _update_state(
            task_id, created_at,
            status="failed",
            stage="failed",
            percent=0,
            message="服务内部错误，请稍后重试",
            error=str(exc),
        )
        return {}


async def _run_pipeline_inner(
    task_id: str,
    url: str,
    page_type: str,
    language: str,
    gbp_url: str,
    created_at: str,
) -> dict[str, Any]:
    """
    Actual pipeline logic. Separated from run_pipeline() so that the
    outer function can wrap it with a catch-all exception handler without
    duplicating error-state logic.
    """

    # ── Stage 1: Scraping (0 → 30 %) ─────────────────────────────────────────
    _update_state(
        task_id, created_at,
        status="scraping",
        stage="loading",
        percent=5,
        message="正在读取页面…",
    )

    from app.tasks.scraper import scrape  # noqa: PLC0415

    try:
        logger.info("Pipeline stage=scraping task_id=%s url=%s gbp_url=%s", task_id, url, gbp_url)
        scrape_result = await scrape(url, gbp_url=gbp_url)
    except RuntimeError as exc:
        logger.error("Scraping failed task_id=%s: %s", task_id, exc)
        _update_state(
            task_id, created_at,
            status="failed",
            stage="failed",
            percent=10,
            message="页面读取失败，请稍后重试",
            error=str(exc),
        )
        return {}

    content: str = scrape_result.get("content", "")
    gbp_data: dict[str, Any] = scrape_result.get("gbp", {})

    # ── Stage 2: Dify workflow (30 → 90 %) ───────────────────────────────────
    logger.info("Pipeline stage=analyzing task_id=%s", task_id)

    from app.tasks.dify_client import call_dify_workflow  # noqa: PLC0415

    _last_written_pct: list[int] = [0]

    async def _progress_cb(stage: str, percent: int, message: str) -> None:
        if percent < 85 and (percent - _last_written_pct[0]) < _PROGRESS_THROTTLE_PCT:
            return
        _last_written_pct[0] = percent
        _update_state(
            task_id, created_at,
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
            gbp_url=gbp_url,
        )
    except RuntimeError as exc:
        logger.error("Dify workflow failed task_id=%s: %s", task_id, exc)
        _update_state(
            task_id, created_at,
            status="failed",
            stage="failed",
            percent=50,
            message="分析失败，请稍后重试",
            error=str(exc),
        )
        return {}

    # ── Stage 3: Done (90 → 100 %) ───────────────────────────────────────────
    logger.info("Pipeline stage=done task_id=%s", task_id)

    final_report: dict[str, Any]
    if isinstance(report, dict):
        score_raw = report.get("score")
        if isinstance(score_raw, str):
            try:
                report["score"] = json.loads(score_raw)
            except json.JSONDecodeError:
                pass
        final_report = report
    else:
        final_report = {"raw": report}

    _update_state(
        task_id, created_at,
        status="done",
        stage="done",
        percent=100,
        message="分析完成",
        result=final_report,
    )

    logger.info("Pipeline complete task_id=%s", task_id)
    return final_report
