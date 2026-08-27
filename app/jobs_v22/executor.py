"""Execution boundary that later v2.2 pipeline milestones will implement."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.errors import DeterministicJobError
from app.report_v22.models import ReportV22


class V22JobExecutor(Protocol):
    async def execute(
        self,
        *,
        job_id: UUID,
        request: dict[str, Any],
        checkpoints: JobCheckpoints,
    ) -> ReportV22: ...


class UnavailableV22Executor:
    """Production-safe placeholder until the real v2.2 pipeline is delivered."""

    async def execute(
        self,
        *,
        job_id: UUID,
        request: dict[str, Any],
        checkpoints: JobCheckpoints | None,
    ) -> ReportV22:
        raise DeterministicJobError(
            "V22_PIPELINE_NOT_READY",
            "SearchTrust v2.2 analysis is not available yet.",
        )
