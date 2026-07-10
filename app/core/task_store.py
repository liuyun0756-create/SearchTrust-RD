"""
app/core/task_store.py
──────────────────────
In-memory task state store with asyncio Queue for SSE streaming.

Replaces Redis for task state management. All state lives in the
FastAPI process memory — no external dependencies required.

Limitations
-----------
- State is lost on process restart / redeploy.
- Not shared across multiple FastAPI instances (use single instance).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Final, Optional

logger = logging.getLogger(__name__)

# Internal sentinel consumed by the API route to emit an SSE keepalive comment.
SSE_HEARTBEAT: Final = object()

# ─────────────────────────────────────────────────────────────────────────────
# In-memory stores
# ─────────────────────────────────────────────────────────────────────────────

# task_id → state dict
_tasks: dict[str, dict[str, Any]] = {}

# task_id → list of asyncio.Queue (one per SSE subscriber)
_subscribers: dict[str, list[asyncio.Queue]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────────────────────

def set_state(task_id: str, state: dict[str, Any]) -> None:
    """Store task state and push to all SSE subscribers."""
    _tasks[task_id] = state

    # Push to all waiting SSE queues
    for q in _subscribers.get(task_id, []):
        try:
            q.put_nowait(state)
        except asyncio.QueueFull:
            pass  # subscriber too slow — skip this event


def get_state(task_id: str) -> Optional[dict[str, Any]]:
    """Return current task state, or None if not found."""
    return _tasks.get(task_id)


def delete_state(task_id: str) -> None:
    """Remove task state and close all subscribers."""
    _tasks.pop(task_id, None)
    for q in _subscribers.pop(task_id, []):
        # Drain the queue first to make room, then send the close signal.
        # This ensures the None sentinel is never dropped due to QueueFull,
        # which would leave SSE subscribers waiting until their configured timeout.
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            q.put_nowait(None)  # signal subscribers to close
        except asyncio.QueueFull:
            pass  # queue maxsize=1 edge case — subscriber will timeout naturally


# ─────────────────────────────────────────────────────────────────────────────
# SSE subscription
# ─────────────────────────────────────────────────────────────────────────────

async def subscribe(
    task_id: str,
    timeout: float = 1260.0,
    heartbeat_interval: float = 20.0,
):
    """
    Async generator that yields state updates for a task.

    Yields the current snapshot immediately, then yields each subsequent
    update pushed by set_state(). Emits an internal heartbeat sentinel while
    the task is quiet, and stops when status is done/failed or the total
    connection timeout elapses.
    """
    # Send current snapshot immediately
    current = get_state(task_id)
    if current:
        yield current
        if current.get("status") in ("done", "failed"):
            return

    # Register a queue for future updates
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers.setdefault(task_id, []).append(q)
    deadline = time.monotonic() + timeout

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                state = await asyncio.wait_for(
                    q.get(),
                    timeout=min(heartbeat_interval, remaining),
                )
            except asyncio.TimeoutError:
                yield SSE_HEARTBEAT
                continue

            if state is None:  # task deleted
                break

            yield state

            if state.get("status") in ("done", "failed"):
                break
    finally:
        # Clean up this subscriber
        subs = _subscribers.get(task_id, [])
        if q in subs:
            subs.remove(q)
        if not subs:
            _subscribers.pop(task_id, None)
