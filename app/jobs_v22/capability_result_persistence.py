"""One bounded atomic completion call for a validated, frozen 2.2.2 diagnosis.

Not mounted in the legacy worker. There is no debit, provider call, regeneration
or client approval here: consume the already-reserved workflow exactly once.
"""
from dataclasses import dataclass
from uuid import UUID

import httpx

from app.jobs_v22.digest import canonical_json_bytes, verified_canonical_json_bytes, verified_request_digest
from app.jobs_v22.errors import DeterministicJobError
from app.jobs_v22.verified_input_resolver import VerifiedRpcClient
from app.report_v22.capability import AnalysisInputManifest, CapabilityReport


@dataclass(frozen=True)
class CapabilityCompletion:
    report_id: UUID
    payload_checksum: str
    idempotent: bool


class SupabaseCapabilityResultPersister:
    def __init__(self, *, url: str, service_role_key: str,
                 http_client: httpx.AsyncClient, max_response_bytes: int = 65_536):
        self.rpc = VerifiedRpcClient(url=url, service_role_key=service_role_key,
            http_client=http_client, max_response_bytes=max_response_bytes,
            prefix="V22_CAPABILITY_PERSISTENCE", invalid_retryable=True,
            total_timeout_seconds=60)

    async def persist(self, *, user_id: UUID, case_id: UUID, job_id: UUID,
                      run_generation: int, manifest: AnalysisInputManifest | dict,
                      report: CapabilityReport | dict) -> CapabilityCompletion:
        try:
            # JSON revalidation also catches model_construct and mutated nested
            # models. SQL independently compares the actual frozen manifest.
            bound = AnalysisInputManifest.model_validate_json(canonical_json_bytes(manifest))
            result = CapabilityReport.model_validate_json(canonical_json_bytes(report))
            canonical = verified_canonical_json_bytes(result.model_dump(mode="json"))
            if (type(run_generation) is not int or run_generation < 1
                    or bound.user_id != user_id or bound.case_id != case_id or bound.job_id != job_id
                    or result.id != job_id or result.input_manifest != bound
                    or bound.mode == "review" or len(canonical) > 2_000_000):
                raise ValueError("Invalid frozen result binding")
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise DeterministicJobError("V22_CAPABILITY_PERSISTENCE_INPUT_INVALID",
                "The diagnosis could not be bound to its saved analysis.") from None
        checksum = verified_request_digest(result.model_dump(mode="json"))
        rows = await self.rpc.post("complete_v22_capability_analysis", {
            "p_user_id": str(user_id), "p_case_id": str(case_id), "p_job_id": str(job_id),
            "p_payload": result.model_dump(mode="json"), "p_canonical_content": canonical.decode("utf-8"),
            "p_run_generation": run_generation,
        })
        row = rows[0] if isinstance(rows, list) and len(rows) == 1 else rows
        if (not isinstance(row, dict) or set(row) != {"report_id", "idempotent", "payload_checksum"}
                or row["report_id"] != str(job_id) or type(row["idempotent"]) is not bool
                or row["payload_checksum"] != checksum):
            # Ack loss/malformed success must be retryable: never refund from
            # this response alone; the transaction may already have committed.
            raise self.rpc.invalid() from None
        return CapabilityCompletion(report_id=job_id, payload_checksum=checksum, idempotent=row["idempotent"])
