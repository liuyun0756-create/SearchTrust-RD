"""
app/models/response.py
──────────────────────
Pydantic response models for the SEO Trust Path Analysis API.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    """Lifecycle states of an analysis task."""

    QUEUED = "queued"          # Accepted, waiting for a Celery worker
    SCRAPING = "scraping"      # Fetching page content via Jina + GBP
    ANALYZING = "analyzing"    # Sending data to Dify workflow
    SCORING = "scoring"        # Dify producing intermediate scores
    REPORTING = "reporting"    # Dify assembling the final report
    DONE = "done"              # Successfully completed
    FAILED = "failed"          # Terminal failure


# ─────────────────────────────────────────────────────────────────────────────
# Sub-models
# ─────────────────────────────────────────────────────────────────────────────

class ProgressInfo(BaseModel):
    """Real-time progress snapshot for a running task."""

    stage: str = Field(
        ...,
        description="Current processing stage name",
        examples=["scraping", "analyzing"],
    )
    percent: int = Field(
        ...,
        ge=0,
        le=100,
        description="Completion percentage (0–100)",
    )
    message: str = Field(
        ...,
        description="Human-readable status message",
        examples=["正在抓取页面内容…"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────────────

class TaskCreateResponse(BaseModel):
    """
    Returned immediately after a client submits an analysis request.

    When ``status == 'done'`` (cache hit), ``result`` is already populated and
    the client does not need to poll ``GET /api/v1/task/{task_id}``.
    Otherwise poll until ``status`` is ``done`` or ``failed``.
    """

    task_id: str = Field(
        ...,
        description="Unique identifier for the created task (UUID4)",
    )
    status: TaskStatus = Field(
        default=TaskStatus.QUEUED,
        description="Initial task status — always 'queued' on creation",
    )
    estimated_seconds: int = Field(
        default=60,
        description="Rough estimate of processing time in seconds",
    )
    result: Optional[Any] = Field(
        default=None,
        description="Final analysis report — only present on cache hit (status='done')",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "task_id": "a3f7c2d1-8b4e-4f9a-b6d0-123456789abc",
                "status": "queued",
                "estimated_seconds": 60,
            }
        }
    }


class TaskStatusResponse(BaseModel):
    """
    Full task snapshot returned by the polling endpoint.

    ``result`` is populated only when ``status == 'done'``.
    ``error`` is populated only when ``status == 'failed'``.
    """

    task_id: str = Field(..., description="Task UUID")
    status: TaskStatus = Field(..., description="Current lifecycle status")
    progress: ProgressInfo = Field(..., description="Stage / percentage / message")
    result: Optional[Any] = Field(
        default=None,
        description="Final analysis report JSON — present only when done",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error description — present only when failed",
    )
    created_at: datetime = Field(..., description="Task creation timestamp (UTC)")
    updated_at: datetime = Field(..., description="Last state-change timestamp (UTC)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "task_id": "a3f7c2d1-8b4e-4f9a-b6d0-123456789abc",
                "status": "analyzing",
                "progress": {
                    "stage": "analyzing",
                    "percent": 45,
                    "message": "正在分析页面内容…",
                },
                "result": None,
                "error": None,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:30Z",
            }
        }
    }


class HealthResponse(BaseModel):
    """Simple health-check payload."""

    status: str = Field(default="ok")
    version: str
    redis: bool = Field(description="True if Redis is reachable")


class ErrorResponse(BaseModel):
    """Standard error envelope."""

    detail: str
    code: Optional[str] = None
