"""Pydantic contracts for SearchTrust API v2.

The runtime routes are introduced in later milestones. V22-003 freezes only
their request and response shapes.
"""

from app.api.v2.models import ApiV2ContractBundle
from app.api.v2.runtime import router

__all__ = ["ApiV2ContractBundle", "router"]
