"""Internal service inputs; legacy parent-required models remain unchanged."""
from uuid import UUID

from app.report_v22.models import StrictModel
from app.report_v22.cross_source_findings_models import CrossSourceFindingsInput
from app.report_v22.findings_models import PublicFindingsInput
from app.report_v22.first_party_findings_models import FirstPartyFindingsInput


class ServiceFirstPartyInput(FirstPartyFindingsInput):
    parent_report_id: UUID | None = None


class ServiceCrossSourceInput(CrossSourceFindingsInput):
    parent_report_id: UUID | None = None
    first_party_input: ServiceFirstPartyInput


class CapabilityServiceInput(StrictModel):
    public_input: PublicFindingsInput
    first_party_input: ServiceFirstPartyInput
