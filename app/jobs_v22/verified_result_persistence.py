"""Validate Verified result closure locally before one atomic persistence RPC."""
from __future__ import annotations

from uuid import UUID

import httpx

from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.verified_input_resolver import VerifiedRpcClient
from app.jobs_v22.verified_models import VerifiedResolvedInput, VerifiedTaskRequest, validate_public_gbp
from app.report_v22.models import ReportV22


class SupabaseVerifiedResultPersister:
    def __init__(self, *, url: str, service_role_key: str, http_client: httpx.AsyncClient,
                 max_response_bytes: int = 65_536):
        self.rpc = VerifiedRpcClient(url=url, service_role_key=service_role_key, http_client=http_client,
            max_response_bytes=max_response_bytes, prefix="V22_VERIFIED_RESULT_PERSISTENCE", invalid_retryable=True)

    async def persist(self, *, job_id: UUID, case_id: UUID, run_generation: int,
                      request: VerifiedTaskRequest, resolved_input: VerifiedResolvedInput, report: ReportV22) -> None:
        try:
            # Dump to Python values to force full validation even for model_construct
            # or mutated instances (model_validate(instance) can otherwise skip it).
            report = ReportV22.model_validate(report.model_dump(mode="python") if isinstance(report, ReportV22) else report)
            resolved_input = VerifiedResolvedInput.model_validate(resolved_input.model_dump(mode="python"))
            resolved_input.validate_request(job_id=job_id, request=request)
            parent = resolved_input.parent_report
            if (type(run_generation) is not int or run_generation < 1 or case_id != request.case_id
                    or report.report_version.report_type != "verified_execution"
                    or report.report_version.report_id != job_id or report.identity != parent.identity
                    or report.report_version.parent_report_id != request.parent_report_id
                    or report.report_version.version_number != parent.report_version.version_number + 1
                    or report.first_party_performance.gsc.snapshot_id != request.gsc_snapshot_id
                    or report.first_party_performance.ga4.snapshot_id != request.ga4_snapshot_id
                    or report.first_party_performance.gbp.snapshot_id is not None
                    or validate_public_gbp(report) != request.public_gbp_snapshot_id):
                raise ValueError("Verified result binding mismatch")
            allowed = {row.source_type: {row.snapshot_id} for row in
                [resolved_input.site_snapshot, resolved_input.serp_snapshot, resolved_input.competitor_snapshot]}
            allowed.update(gbp={request.public_gbp_snapshot_id}, gsc={request.gsc_snapshot_id}, ga4={request.ga4_snapshot_id})
            public_ids = set().union(*(allowed[source] for source in ("site", "serp", "competitor", "gbp")))
            for alias in ("coverage", "pagespeed"):
                inherited = {e.snapshot_id for e in parent.evidence_index if e.source_type == alias}
                for source in parent.data_coverage.sources:
                    if source.source_type == alias:
                        inherited.update(source.snapshot_ids)
                allowed[alias] = inherited & public_ids
            if any(e.snapshot_id not in allowed[e.source_type] for e in report.evidence_index):
                raise ValueError("result evidence binding mismatch")
            coverage = {item.source_type: item.snapshot_ids for item in report.data_coverage.sources}
            for source in ("site", "serp", "competitor", "gbp", "gsc", "ga4"):
                if source not in coverage or len(coverage[source]) != 1 or set(coverage[source]) != allowed[source]:
                    raise ValueError("result coverage binding mismatch")
            if any(not set(ids) <= allowed[source] for source, ids in coverage.items()):
                raise ValueError("result derived coverage binding mismatch")
        except (ValueError, TypeError, AttributeError, KeyError):
            raise DeterministicJobError("V22_VERIFIED_RESULT_INVALID", "The Verified report did not pass validation.") from None
        rows = await self.rpc.post("persist_v22_verified_result", {
            "p_job_id": str(job_id), "p_case_id": str(case_id),
            "p_report_payload": report.model_dump(mode="json"), "p_run_generation": run_generation})
        row = rows[0] if isinstance(rows, list) and len(rows) == 1 else rows
        if (not isinstance(row, dict) or set(row) != {"report_id", "idempotent"}
                or row["report_id"] != str(job_id) or type(row["idempotent"]) is not bool):
            raise self.rpc.invalid() from None
