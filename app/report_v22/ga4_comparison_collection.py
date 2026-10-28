"""Internal saved-record planner/collector, separate from durable authorization."""
from uuid import UUID
from zoneinfo import ZoneInfo

from app.google_connections_v22.ga4_comparison import Ga4ComparisonProvider
from app.jobs_v22.cost_ledger import JobCostLedger
from app.jobs_v22.digest import verified_request_digest, verified_canonical_json_bytes
from app.report_v22.capability import instant
from app.report_v22.comparison_windows import WindowDays, plan_windows
from app.report_v22.ga4_comparison_models import (
    Ga4ComparisonScope, Ga4ComparisonPlan, Ga4ComparisonCollection, qualify_ga4,
)
from app.report_v22.implementation_records import ImplementationRecord, implementation_reference


def prepare_ga4_comparison(record: ImplementationRecord, *, action_id: str, days: WindowDays,
                           binding_id: UUID, scope: Ga4ComparisonScope, at: str) -> Ga4ComparisonPlan:
    # Callers must resolve the authorized record, binding and scope before use.
    record = ImplementationRecord.model_validate_json(record.model_dump_json())
    scope = Ga4ComparisonScope.model_validate_json(scope.model_dump_json())
    if instant(at) < instant(record.recorded_at): raise ValueError("preparation predates implementation record")
    entry = next((item for item in record.entries if item.action_id == action_id), None)
    if entry is None: raise ValueError("action has no saved implementation")
    windows = plan_windows(entry, scope.timezone, days)
    elapsed = instant(at).astimezone(ZoneInfo(scope.timezone)).date() > windows.after.end
    body = dict(schema_version="v22_ga4_comparison_plan_v1", user_id=str(record.user_id), case_id=str(record.case_id),
        report_id=str(record.report_id), delivery_revision_id=str(record.delivery_revision_id),
        implementation=implementation_reference(record).model_dump(mode="json"), action_id=action_id,
        binding_id=str(binding_id), scope=scope.model_dump(mode="json"), prepared_at=at,
        windows=windows.model_dump(mode="json"), state="collectable" if elapsed else "waiting",
        expected_report_requests=6 if elapsed else 0, max_configuration_requests=20 if elapsed else 0)
    body["checksum"] = verified_request_digest(body)
    return Ga4ComparisonPlan.model_validate_json(verified_canonical_json_bytes(body))


async def collect_ga4_comparison(record: ImplementationRecord, *, action_id: str, days: WindowDays,
                                 binding_id: UUID, scope: Ga4ComparisonScope, at: str,
                                 provider: Ga4ComparisonProvider, token: str,
                                 cost_ledger: JobCostLedger) -> Ga4ComparisonCollection:
    plan = prepare_ga4_comparison(record, action_id=action_id, days=days, binding_id=binding_id, scope=scope, at=at)
    snapshot = await provider.collect_exact(plan, token, cost_ledger=cost_ledger) if plan.state == "collectable" else None
    qualification = qualify_ga4(snapshot) if snapshot else None
    body = dict(schema_version="v22_ga4_comparison_collection_v1", plan=plan.model_dump(mode="json"),
        state="collected" if snapshot else "waiting", snapshot=snapshot.model_dump(mode="json") if snapshot else None,
        qualification=qualification.model_dump(mode="json") if qualification else None)
    body["checksum"] = verified_request_digest(body)
    return Ga4ComparisonCollection.model_validate_json(verified_canonical_json_bytes(body))
