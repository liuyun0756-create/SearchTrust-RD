"""Versioned provenance for inheriting a Case's existing human confirmation."""
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.report_v22.models import StrictModel

RULESET = "v22_capability_public_v21"
MANIFEST_SCHEMA = "v22_analysis_input_v11"
REPORT_SCHEMA = "2.2.16"
BaseRuleset = Literal[
    "v22_capability_public_v13", "v22_capability_public_v14", "v22_capability_public_v15",
    "v22_capability_public_v16", "v22_capability_public_v17", "v22_capability_public_v18",
    "v22_capability_public_v20",
]
BASE_VERSIONS = {
    "v22_capability_public_v13": ("v22_analysis_input_v3", "2.2.8"),
    "v22_capability_public_v14": ("v22_analysis_input_v4", "2.2.9"),
    "v22_capability_public_v15": ("v22_analysis_input_v5", "2.2.10"),
    "v22_capability_public_v16": ("v22_analysis_input_v6", "2.2.11"),
    "v22_capability_public_v17": ("v22_analysis_input_v7", "2.2.12"),
    "v22_capability_public_v18": ("v22_analysis_input_v8", "2.2.13"),
    "v22_capability_public_v20": ("v22_analysis_input_v10", "2.2.15"),
}


class IdentityInheritance(StrictModel):
    base_ruleset: BaseRuleset
    source_identity_version_id: UUID
    source_identity_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    source_revision: int = Field(ge=1)
    confirmed_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
    branch: Literal["retained", "needs_confirmation"]
    operating_model: Literal["retained", "needs_confirmation"]
    visits: Literal["retained", "needs_confirmation", "not_previously_confirmed"]
