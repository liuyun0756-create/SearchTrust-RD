"""Durable task infrastructure for the isolated SearchTrust API v2 runtime."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.jobs_v22.models import JobErrorState, JobState

__all__ = ["JobErrorState", "JobState"]


def __getattr__(name: str):
    # Request contracts can load independently of runtime models and API stages.
    if name in __all__:
        from app.jobs_v22 import models

        return getattr(models, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
