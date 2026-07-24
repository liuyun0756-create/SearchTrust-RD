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

import asyncio
import json
import logging
import re
import time
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
    input_page_type = page_type
    language = "English"

    # ── Stage 1: Scraping (0 → 30 %) ─────────────────────────────────────────
    _update_state(
        task_id, created_at,
        status="scraping",
        stage="loading",
        percent=5,
        message="Reading the page...",
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
            message="The page could not be read. Please try again.",
            error=str(exc),
        )
        return {}

    content: str = scrape_result.get("content", "")
    gbp_data: dict[str, Any] = scrape_result.get("gbp", {})
    final_gbp_url: str = scrape_result.get("gbp_url") or gbp_url or ""

    # ── Stage 2: Dify workflow (30 → 90 %) ───────────────────────────────────
    logger.info("Pipeline stage=analyzing task_id=%s", task_id)

    from app.tasks.dify_client import call_dify_workflow  # noqa: PLC0415
    from app.models.request import resolve_page_type  # noqa: PLC0415
    from app.report_v21.evidence_ledger import build_evidence_ledger  # noqa: PLC0415
    from app.report_v21.action_requirements import serialize_action_requirements  # noqa: PLC0415
    from app.report_v21.gbp_rule_evaluator import evaluate_gbp_rules  # noqa: PLC0415
    from app.report_v21.normalize import (  # noqa: PLC0415
        normalize_native_report_to_v21,
        normalize_report_copy_to_v21,
    )
    from app.report_v21.page_facts import build_page_facts  # noqa: PLC0415
    from app.report_v21.review_corpus import (  # noqa: PLC0415
        build_review_corpus,
        serialize_review_corpus,
    )
    from app.report_v21.rule_contract import (  # noqa: PLC0415
        parse_rule_results,
        validate_english_narrative,
    )

    # Resolve English page_type to the Chinese value Dify expects
    dify_page_type = resolve_page_type(input_page_type)
    dify_gbp_data = _build_dify_gbp_payload(gbp_data)
    page_facts = build_page_facts(content, scrape_result.get("business"))
    review_corpus = build_review_corpus(
        content,
        gbp_data,
        page_url=url,
        gbp_url=final_gbp_url,
    )

    v21_context = {
        "task_id": task_id,
        "url": url,
        "page_type": input_page_type,
        "dify_page_type": dify_page_type,
        "generated_at": created_at,
        "input_gbp_url": gbp_url,
        "gbp_url": final_gbp_url,
        "gbp_data": gbp_data,
        "page_content": content,
        "content": content,
        "page_business": scrape_result.get("business"),
        "business": scrape_result.get("business"),
        "schema_data": scrape_result.get("schema"),
        "content_checked": bool(content),
        "scraper_source": scrape_result.get("scraper_source"),
        "sub_pages": scrape_result.get("sub_pages"),
        "raw_content_length": scrape_result.get("raw_content_length"),
        "gbp_lookup_attempted": scrape_result.get("gbp_lookup_attempted"),
        "gbp_error": scrape_result.get("gbp_error"),
        "gbp_lookup_diagnostic": scrape_result.get("gbp_lookup_diagnostic"),
        "page_facts": page_facts,
        "review_corpus": review_corpus,
    }
    evidence_ledger = build_evidence_ledger(v21_context)
    backend_gbp_results, backend_gbp_applicability, backend_gbp_findings = evaluate_gbp_rules(v21_context)
    v21_context["backend_gbp_findings"] = backend_gbp_findings
    action_requirements = serialize_action_requirements()
    serialized_review_corpus = serialize_review_corpus(review_corpus)
    validated_report: dict[str, Any] = {}

    def _validate_dify_output(outputs: dict[str, Any]) -> None:
        if "rule_results" in outputs or "report_copy_v2_1" in outputs:
            rule_results, rule_applicability = parse_rule_results(
                outputs,
                backend_gbp_results=backend_gbp_results,
                backend_gbp_applicability=backend_gbp_applicability,
            )
            normalized = normalize_report_copy_to_v21(
                outputs,
                v21_context,
                rule_results,
                rule_applicability,
                evidence_ledger,
            )
            v21_context.update({
                "rule_results": rule_results,
                "rule_applicability": rule_applicability,
            })
        else:
            # Deployment bridge: an already-published legacy workflow can keep
            # serving until the user manually imports the new Dify candidate.
            # It does not receive the new determinism guarantee.
            validate_english_narrative(outputs)
            normalized = normalize_native_report_to_v21(outputs, v21_context)
        validated_report.clear()
        validated_report.update(normalized)

    _last_written_pct: list[int] = [0]
    _last_written_ts: list[float] = [0.0]

    async def _progress_cb(stage: str, percent: int, message: str) -> None:
        now = time.monotonic()
        # 低于 85%：进度变化不足 10% 则跳过
        if percent < 85 and (percent - _last_written_pct[0]) < _PROGRESS_THROTTLE_PCT:
            return
        # 85% 以上：距上次推送不足 3 秒则跳过（防止心跳刷屏）
        if percent >= 85 and (now - _last_written_ts[0]) < 3.0:
            return
        _last_written_pct[0] = percent
        _last_written_ts[0] = now
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
            page_type=dify_page_type,
            language=language,
            content=content,
            gbp_data=dify_gbp_data,
            task_id=task_id,
            progress_callback=_progress_cb,
            gbp_url=final_gbp_url,
            output_validator=_validate_dify_output,
            page_facts=page_facts,
            review_corpus=serialized_review_corpus,
            backend_gbp_findings=backend_gbp_findings,
            action_requirements=action_requirements,
        )
    except RuntimeError as exc:
        logger.error("Dify workflow failed task_id=%s: %s", task_id, exc)
        error_code = str(getattr(exc, "error_code", "DIFY_WORKFLOW_FAILED"))
        validation_errors = list(
            getattr(exc, "details", None)
            or getattr(exc, "validation_errors", None)
            or []
        )
        error_result = {
            "status": "failed",
            "error_code": error_code,
            "retryable": True,
            "user_message": str(exc),
            "validation_errors": validation_errors,
            "task_id": task_id,
        }
        _update_state(
            task_id, created_at,
            status="failed",
            stage="failed",
            percent=50,
            message="The analysis could not be completed. Please try again.",
            result=error_result,
            error=str(exc),
        )
        return error_result

    # ── Stage 3: Done (90 → 100 %) ───────────────────────────────────────────
    logger.info("Pipeline stage=done task_id=%s", task_id)

    final_report: dict[str, Any]
    if isinstance(report, dict):
        # score：剥掉 Markdown 代码块再 parse
        score_raw = report.get("score")
        if isinstance(score_raw, str):
            try:
                score_str = re.sub(r"^```json\s*|\s*```$", "", score_raw.strip())
                report["score"] = json.loads(score_str)
            except json.JSONDecodeError:
                pass

        # trust_status / ranking_potential / risk_level：JSON 字符串直接 parse
        for key in ("trust_status", "ranking_potential", "risk_level"):
            val = report.get(key)
            if isinstance(val, str):
                try:
                    report[key] = json.loads(val)
                except json.JSONDecodeError:
                    pass

        final_report = report
    else:
        final_report = {"raw": report}

    # gbp_data 有内容返回 true，空则返回 false，不暴露原始数据
    final_report["gbp_connected"] = bool(gbp_data)

    try:
        from app.report_v21.normalize import (  # noqa: PLC0415
            ReportV21OutputInvalid,
            normalize_native_report_to_v21,
        )

        normalized_v21 = validated_report or normalize_native_report_to_v21(final_report, v21_context)
        final_report["report_v2_1"] = normalized_v21["report_v2_1"]
        final_report["gbp_connected"] = (
            final_report["report_v2_1"].get("gbp_status", {}).get("status") == "checked"
        )
    except ReportV21OutputInvalid as exc:
        error_result = exc.to_result(task_id)
        logger.warning(
            "Native report_v2_1 invalid task_id=%s errors=%s warnings=%s",
            task_id,
            exc.validation_errors,
            exc.warnings,
        )
        _update_state(
            task_id, created_at,
            status="failed",
            stage="failed",
            percent=100,
            message=exc.user_message,
            result=error_result,
            error=exc.user_message,
        )
        return error_result
    except Exception as exc:  # noqa: BLE001
        error_result = {
            "status": "failed",
            "error_code": "V21_NORMALIZATION_ERROR",
            "retryable": True,
            "user_message": "The report could not be completed because the structured report validation failed. Please try again.",
            "validation_errors": [str(exc)],
            "task_id": task_id,
        }
        logger.warning("native report_v2_1 normalization failed task_id=%s: %s", task_id, exc)
        _update_state(
            task_id, created_at,
            status="failed",
            stage="failed",
            percent=100,
            message=error_result["user_message"],
            result=error_result,
            error=error_result["user_message"],
        )
        return error_result

    _update_state(
        task_id, created_at,
        status="done",
        stage="done",
        percent=100,
        message="Analysis complete",
        result=final_report,
    )

    logger.info("Pipeline complete task_id=%s", task_id)
    return final_report


def _build_dify_gbp_payload(gbp_data: dict[str, Any]) -> dict[str, Any]:
    """Keep backend audit metadata out of the Dify prompt payload."""
    if not isinstance(gbp_data, dict) or not gbp_data:
        return {}
    allowed_keys = (
        "name",
        "address",
        "phone",
        "rating",
        "reviews",
        "type",
        "hours",
        "website",
        "service_areas",
        "data_id",
    )
    payload = {key: gbp_data.get(key) for key in allowed_keys if key in gbp_data}
    return payload
