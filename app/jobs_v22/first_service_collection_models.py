"""Public collection intent, not a Prospect or a parent-required legacy report."""
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.api.v2.models import ConfirmedCompetitor, GenerationLimits
from app.competitors_v22.selection import AnalysisDiscoveryLink
from app.report_v22.models import BusinessIdentity, StrictModel, TargetMarket


class FirstServiceCollectionRequest(StrictModel):
    case_id: UUID
    business_identity: BusinessIdentity
    primary_service: str = Field(min_length=1, max_length=200)
    target_market: TargetMarket
    queries: list[str] = Field(min_length=3, max_length=5)
    competitors: list[ConfirmedCompetitor] = Field(min_length=1, max_length=3)
    generation_limits: GenerationLimits = Field(default_factory=GenerationLimits)

    @model_validator(mode="after")
    def distinct_context(self):
        if (len({q.casefold() for q in self.queries}) != len(self.queries)
                or any(not q.strip() for q in self.queries)
                or len({c.competitor_id for c in self.competitors}) != len(self.competitors)
                or len({str(c.website_url).casefold() for c in self.competitors}) != len(self.competitors)
                or any(c.confirmation_source != "user" for c in self.competitors)
                or self.generation_limits.competitor_count != len(self.competitors)
                or self.business_identity.public_gbp_url is None):
            raise ValueError("Invalid confirmed collection context")
        return self


class FirstServiceCollectionEnvelope(StrictModel):
    schema_version: Literal["v22_first_service_collection_v1"]
    collection_request: FirstServiceCollectionRequest
    competitor_discovery: AnalysisDiscoveryLink
