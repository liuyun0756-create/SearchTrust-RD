"""
app/tasks/dify_client.py
────────────────────────
Dify workflow client with SSE streaming support.

The client posts to ``POST /v1/workflows/run`` with ``response_mode=streaming``,
streams the SSE response, and fires a progress callback for each relevant event.
Final result is extracted from the ``workflow_finished`` event.

Public API
----------
call_dify_workflow(inputs, task_id, progress_callback) → dict | None
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.core.config import settings
from app.core.rate_limiter import dify_rate_limiter

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Type aliases
# ─────────────────────────────────────────────────────────────────────────────
ProgressCallback = Callable[[str, int, str], Awaitable[None]]
"""
Async callable signature: (stage: str, percent: int, message: str) -> None
"""


# ─────────────────────────────────────────────────────────────────────────────
# SSE parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_sse_line(line: str) -> Optional[dict[str, Any]]:
    """
    Parse a single SSE ``data:`` line into a Python dict.

    Ignores comment lines and empty lines.

    Parameters
    ----------
    line:
        A raw line from the HTTP streaming response.

    Returns
    -------
    Parsed JSON dict or None if the line should be skipped.
    """
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload == "[DONE]":
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            logger.debug("SSE JSON parse error: %s — raw=%r", exc, payload)
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Progress mapping
# ─────────────────────────────────────────────────────────────────────────────

# Map Dify event names → (stage, percent, message)
_EVENT_PROGRESS: dict[str, tuple[str, int, str]] = {
    "workflow_started":  ("analyzing",  30, "Dify 工作流已启动，开始分析…"),
    "node_started":      ("analyzing",  45, "正在分析页面内容…"),
    "node_finished":     ("scoring",    70, "规则评分完成，生成报告中…"),
    "workflow_finished": ("reporting",  90, "报告生成完毕，整理结果…"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Core Dify caller
# ─────────────────────────────────────────────────────────────────────────────

async def _stream_workflow(
    inputs: dict[str, Any],
    task_id: str,
    progress_callback: Optional[ProgressCallback],
) -> Optional[dict[str, Any]]:
    """
    Execute one Dify streaming request and return the final outputs dict.

    Parameters
    ----------
    inputs:
        Key-value pairs forwarded as ``inputs`` in the Dify request body.
    task_id:
        Used as ``user`` in the Dify payload (for traceability).
    progress_callback:
        Optional async function called on each notable SSE event.

    Returns
    -------
    dict from ``data.outputs`` of the ``workflow_finished`` event,
    or None if the workflow ended without outputs.

    Raises
    ------
    httpx.HTTPError on network / HTTP errors.
    RuntimeError on unexpected stream termination.
    """
    endpoint = f"{settings.dify_api_url_str}/workflows/run"
    headers = {
        "Authorization": f"Bearer {settings.DIFY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body: dict[str, Any] = {
        "inputs": inputs,
        "response_mode": "streaming",
        "user": task_id,
    }

    logger.info(
        "Dify workflow request — task_id=%s endpoint=%s",
        task_id,
        endpoint,
    )

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=15.0,
            read=float(settings.DIFY_STREAM_TIMEOUT),
            write=15.0,
            pool=5.0,
        ),
        follow_redirects=True,
    ) as client:
        async with client.stream("POST", endpoint, headers=headers, json=body) as resp:
            resp.raise_for_status()

            final_outputs: Optional[dict[str, Any]] = None
            node_count = 0

            async for raw_line in resp.aiter_lines():
                event = _parse_sse_line(raw_line)
                if event is None:
                    continue

                event_type: str = event.get("event", "")
                event_data: dict[str, Any] = event.get("data", {})

                logger.debug(
                    "SSE event — task_id=%s event=%s",
                    task_id,
                    event_type,
                )

                # ── node_started / node_finished: track node progress ─────────
                if event_type == "node_started":
                    node_count += 1
                    if progress_callback:
                        await progress_callback(
                            "analyzing",
                            min(30 + node_count * 5, 65),
                            f"节点 {node_count} 开始处理…",
                        )

                elif event_type == "node_finished":
                    node_title = event_data.get("title", f"节点 {node_count}")
                    if progress_callback:
                        await progress_callback(
                            "scoring",
                            min(65 + node_count * 3, 85),
                            f"节点「{node_title}」完成",
                        )

                elif event_type == "workflow_started":
                    if progress_callback:
                        await progress_callback("analyzing", 30, "Dify 工作流已启动…")

                elif event_type == "workflow_finished":
                    if progress_callback:
                        await progress_callback("reporting", 90, "工作流完成，整理最终报告…")

                    # ── Extract outputs ───────────────────────────────────────
                    outputs = event_data.get("outputs")
                    if isinstance(outputs, dict):
                        final_outputs = outputs
                    elif isinstance(outputs, str):
                        # Some Dify versions return outputs as a JSON string
                        try:
                            final_outputs = json.loads(outputs)
                        except json.JSONDecodeError:
                            final_outputs = {"raw": outputs}

                    logger.info(
                        "workflow_finished received — task_id=%s outputs_keys=%s",
                        task_id,
                        list(final_outputs.keys()) if final_outputs else None,
                    )
                    break  # stream is done

                elif event_type == "error":
                    error_msg = event_data.get("message", "Unknown Dify error")
                    logger.error(
                        "Dify stream error — task_id=%s: %s",
                        task_id,
                        error_msg,
                    )
                    raise RuntimeError(f"Dify workflow error: {error_msg}")

    return final_outputs


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point with retry
# ─────────────────────────────────────────────────────────────────────────────

async def call_dify_workflow(
    url: str,
    page_type: str,
    language: str,
    content: str,
    gbp_data: dict[str, Any],
    task_id: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> dict[str, Any]:
    """
    Call the Dify SEO analysis workflow and return the final report.

    Retries up to ``DIFY_RETRY`` times with exponential back-off.

    Parameters
    ----------
    url:
        Original page URL.
    page_type:
        Page type classification string.
    language:
        Requested report language.
    content:
        Scraped page text from Jina Reader.
    gbp_data:
        Google Business Profile data dict.
    task_id:
        Used as ``user`` identifier in the Dify request.
    progress_callback:
        Optional async (stage, percent, message) → None.

    Returns
    -------
    dict — the final analysis report (from Dify ``outputs``).

    Raises
    ------
    RuntimeError if all retry attempts fail.
    """
    inputs: dict[str, Any] = {
        "url": url,
        "page_type": page_type,
        "language": language,
        "content": content,
        "gbp_data": json.dumps(gbp_data, ensure_ascii=False),
    }

    last_exc: Exception = RuntimeError("No attempts made")

    for attempt in range(1, settings.DIFY_RETRY + 1):
        try:
            logger.info(
                "Dify attempt %d/%d — task_id=%s",
                attempt,
                settings.DIFY_RETRY,
                task_id,
            )

            # ── Global rate limit: acquire a token before calling Dify ────────
            # This is enforced across ALL Celery workers via Redis, so we never
            # exceed the configured RPM cap regardless of concurrency.
            try:
                await dify_rate_limiter.async_acquire()
            except TimeoutError as exc:
                logger.error(
                    "Dify rate limiter timeout task_id=%s: %s — "
                    "consider raising DIFY_RPM_CAPACITY or your API quota",
                    task_id, exc,
                )
                raise RuntimeError(
                    f"Dify call rejected by rate limiter (system overloaded): {exc}"
                ) from exc

            result = await _stream_workflow(inputs, task_id, progress_callback)

            if result is None:
                raise RuntimeError(
                    "Dify workflow_finished event received no outputs"
                )

            logger.info(
                "Dify workflow completed — task_id=%s attempt=%d",
                task_id,
                attempt,
            )
            return result

        except (httpx.HTTPError, RuntimeError, asyncio.TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                "Dify attempt %d/%d failed — task_id=%s: %s",
                attempt,
                settings.DIFY_RETRY,
                task_id,
                exc,
            )
            if attempt < settings.DIFY_RETRY:
                wait = 2 ** attempt       # 2s, 4s, 8s …
                logger.debug("Retrying Dify in %ds…", wait)
                await asyncio.sleep(wait)

    raise RuntimeError(
        f"Dify workflow failed after {settings.DIFY_RETRY} attempts "
        f"for task_id={task_id}: {last_exc}"
    )
