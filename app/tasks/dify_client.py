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
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.core.config import settings

# ── In-process token bucket for Dify RPM limiting ────────────────────────────
class _TokenBucket:
    """Simple in-process token bucket — no Redis required."""

    def __init__(self, capacity: int, refill_amount: int, refill_interval: int) -> None:
        self._capacity = capacity
        self._refill_amount = refill_amount
        self._refill_interval = refill_interval
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        elapsed = now - self._last_refill
        refills = int(elapsed / self._refill_interval)
        if refills > 0:
            self._tokens = min(self._capacity, self._tokens + refills * self._refill_amount)
            self._last_refill += refills * self._refill_interval
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    async def async_acquire(self, max_wait: float = 120.0) -> None:
        deadline = time.monotonic() + max_wait
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                raise TimeoutError("Dify rate limiter timed out")
            await asyncio.sleep(1.0)

    async def async_try_acquire(self) -> bool:
        return self.try_acquire()


_dify_bucket = _TokenBucket(
    capacity=settings.DIFY_RPM_CAPACITY,
    refill_amount=settings.DIFY_RPM_REFILL,
    refill_interval=settings.DIFY_RPM_INTERVAL,
)

logger = logging.getLogger(__name__)


class _RetryablePluginError(Exception):
    """Raised when a Dify node returns a transient 5xx error (e.g. 502 from LLM gateway)."""

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
                            "正在分析中…",
                        )

                elif event_type == "node_finished":
                    if progress_callback:
                        await progress_callback(
                            "scoring",
                            min(65 + node_count * 3, 85),
                            "正在分析中…",
                        )

                elif event_type == "workflow_started":
                    if progress_callback:
                        await progress_callback("analyzing", 30, "正在分析中…")

                elif event_type == "workflow_finished":
                    if progress_callback:
                        await progress_callback("reporting", 90, "正在生成报告…")

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
                    error_type = event_data.get("error_type", "")
                    logger.error(
                        "Dify stream error — task_id=%s type=%s: %s",
                        task_id,
                        error_type,
                        error_msg,
                    )
                    # PluginInvokeError containing 502/503/504 is a transient
                    # gateway error from the LLM provider — signal caller to retry
                    # by raising a dedicated sentinel so the retry loop can catch it.
                    is_retryable_plugin_error = (
                        error_type == "InvokeError"
                        and any(
                            f"status code {code}" in error_msg
                            for code in ("502", "503", "504")
                        )
                    )
                    if is_retryable_plugin_error:
                        raise _RetryablePluginError(
                            f"Dify PluginInvokeError (retryable): {error_msg}"
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
    gbp_url: str = "",
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
    gbp_url:
        Google Business Profile URL, passed through to the final report.

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
        "gbp_url": gbp_url,
    }

    # Network-level transient errors worth retrying immediately (with a short
    # sleep) because they are usually caused by brief connectivity hiccups:
    #   - httpx.TimeoutException    — read/connect timeout
    #   - httpx.ConnectError        — TCP connection refused / reset
    #   - httpx.RemoteProtocolError — server closed connection mid-stream
    #
    # HTTP 5xx errors from the LLM gateway (502 Bad Gateway, 503 Service
    # Unavailable, 504 Gateway Timeout) are also transient and worth retrying.
    #
    # All other failures (rate-limiter timeout, 4xx HTTP status, missing
    # outputs, RuntimeError from Dify) are re-raised immediately so the
    # pipeline can surface them and mark the task as failed.
    _TRANSIENT_ERRORS = (
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
    )
    # 5xx status codes that indicate a transient gateway/server error
    _RETRYABLE_STATUS_CODES = {502, 503, 504}

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
            if not await _dify_bucket.async_try_acquire():
                logger.warning(
                    "Dify rate limiter bucket empty — task_id=%s waiting for token",
                    task_id,
                )
                await _dify_bucket.async_acquire()

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

        except httpx.HTTPStatusError as exc:
            # Retry on 5xx gateway errors (502/503/504), raise immediately on others
            if exc.response.status_code in _RETRYABLE_STATUS_CODES:
                last_exc = exc
                logger.warning(
                    "Dify HTTP %d (retryable) attempt %d/%d — task_id=%s: %s",
                    exc.response.status_code,
                    attempt,
                    settings.DIFY_RETRY,
                    task_id,
                    exc,
                )
                if attempt < settings.DIFY_RETRY:
                    wait = 2 ** attempt   # 2s, 4s, 8s
                    logger.info("Retrying Dify in %ds…", wait)
                    await asyncio.sleep(wait)
            else:
                logger.warning(
                    "Dify HTTP %d (non-retryable) attempt %d/%d — task_id=%s (not retrying)",
                    exc.response.status_code,
                    attempt,
                    settings.DIFY_RETRY,
                    task_id,
                )
                raise

        except (_RetryablePluginError, httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            # Transient network error — short sleep then retry within this call.
            # Sleep is brief (≤8 s) so the worker is not blocked for long.
            last_exc = exc
            logger.warning(
                "Dify transient error attempt %d/%d — task_id=%s: %s",
                attempt,
                settings.DIFY_RETRY,
                task_id,
                exc,
            )
            if attempt < settings.DIFY_RETRY:
                wait = 2 ** attempt   # 2s, 4s, 8s
                logger.debug("Retrying Dify in %ds…", wait)
                await asyncio.sleep(wait)

        except (RuntimeError, asyncio.TimeoutError) as exc:
            # Non-transient failure (bad payload, missing outputs, logic error).
            # Raise immediately so the pipeline can mark the task as failed.
            logger.warning(
                "Dify non-transient error attempt %d/%d — task_id=%s: %s (not retrying inline)",
                attempt,
                settings.DIFY_RETRY,
                task_id,
                exc,
            )
            raise

    raise RuntimeError(
        f"Dify workflow failed after {settings.DIFY_RETRY} attempts "
        f"for task_id={task_id}: {last_exc}"
    )
