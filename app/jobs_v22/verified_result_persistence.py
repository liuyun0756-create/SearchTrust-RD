"""Validate Verified result closure locally before one atomic persistence RPC."""
from __future__ import annotations

from uuid import UUID

import httpx

from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.verified_input_resolver import (
    TrustedVerifiedInput, VerifiedRpcClient, require_trusted_verified_input,
)
from app.jobs_v22.verified_models import VerifiedResolvedInput, VerifiedTaskRequest, validate_public_gbp
from app.report_v22.models import ReportV22
from app.report_v22.version_diff_identity import finding_fingerprint

PERSISTENCE_TIMEOUT_SECONDS = 60


class SupabaseVerifiedResultPersister:
    def __init__(self, *, url: str, service_role_key: str, http_client: httpx.AsyncClient,
                 max_response_bytes: int = 65_536):
        self.rpc = VerifiedRpcClient(url=url, service_role_key=service_role_key, http_client=http_client,
            max_response_bytes=max_response_bytes, prefix="V22_VERIFIED_RESULT_PERSISTENCE",
            invalid_retryable=True, total_timeout_seconds=PERSISTENCE_TIMEOUT_SECONDS)

    async def persist(self, *, job_id: UUID, case_id: UUID, run_generation: int,
                      request: VerifiedTaskRequest, resolved_input: TrustedVerifiedInput, report: ReportV22) -> None:
        try:
            # Dump to Python values to force full validation even for model_construct
            # or mutated instances (model_validate(instance) can otherwise skip it).
            report = ReportV22.model_validate(report.model_dump(mode="python") if isinstance(report, ReportV22) else report)
            # Provenance belongs to the resolver-created capability, never to
            # Pydantic state that could be copied or reconstructed by a caller.
            payload = require_trusted_verified_input(
                resolved_input, run_generation=run_generation
            )
            resolved_input = VerifiedResolvedInput.model_validate(payload.model_dump(mode="python"))
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
            parent_findings = {finding.finding_id: finding for finding in parent.findings}
            referenced_previous: set[str] = set()
            for entry in report.version_diff.entries:
                if entry.change_type == "new":
                    continue
                previous = entry.previous_finding
                finding = parent_findings.get(previous.finding_id)
                if (previous.report_id != request.parent_report_id or finding is None
                        or previous.finding_id in referenced_previous or previous.statement != finding.statement
                        or previous.fingerprint != finding_fingerprint(finding)):
                    raise ValueError("previous Finding does not match the frozen parent")
                referenced_previous.add(previous.finding_id)
            allowed = {row.source_type: {row.snapshot_id} for row in
                [resolved_input.site_snapshot, resolved_input.serp_snapshot, resolved_input.competitor_snapshot]}
            allowed.update(gbp={request.public_gbp_snapshot_id}, gsc={request.gsc_snapshot_id}, ga4={request.ga4_snapshot_id})
            public_ids = set().union(*(allowed[source] for source in ("site", "serp", "competitor", "gbp")))
            for alias in ("coverage", "pagespeed"):
                # Derived public evidence may be created during verified rebuild even
                # when the compact parent report did not retain that alias row.
                allowed[alias] = public_ids
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
