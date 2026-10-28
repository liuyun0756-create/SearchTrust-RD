"""Local cross-language acceptance harness. JSON-lines IPC only; no live services.

Saved source fixtures stand in for collectors. Input freezing, immutable input
resolution, report rules and completion use production code and real SQL RPCs
provided by the parent test process. Never import this from application code.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
from fakeredis.aioredis import FakeRedis
from app.jobs_v22.capability_executor import CapabilityV22Executor
from app.jobs_v22.capability_input_repository import SupabaseCapabilityInputRepository
from app.jobs_v22.capability_result_persistence import SupabaseCapabilityResultPersister
from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.digest import canonical_json_bytes, request_digest, verified_request_digest
from app.jobs_v22.errors import TransientJobError
from app.report_v22.capability import AnalysisInputManifest
from app.report_v22.capability_service_inputs import CapabilityServiceInput
from test_v22_capability_service import inputs
from test_v22_competitor_collection_stage import request as collection_request


def emit(value):
    print(canonical_json_bytes(value).decode(), flush=True)


def seed(clean):
    manifest, request, _ = inputs(clean=clean)
    # Move synthetic observation windows together, never real captured evidence.
    delta = datetime.now(timezone.utc).date() - datetime.fromisoformat(manifest.evaluated_at.replace("Z", "+00:00")).date()

    def shift(value):
        if isinstance(value, dict): return {k: shift(v) for k, v in value.items()}
        if isinstance(value, list): return [shift(v) for v in value]
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return (date.fromisoformat(value) + delta).isoformat()
        if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}T", value):
            return (datetime.fromisoformat(value.replace("Z", "+00:00")) + delta).isoformat().replace("+00:00", "Z")
        return value

    m, r = shift(manifest.model_dump(mode="json")), shift(request.model_dump(mode="json"))
    # Keep evaluation and source observations in the past even before the fixture's hour.
    now = datetime.now(timezone.utc) - timedelta(minutes=2)
    offset = now - datetime.fromisoformat(m["evaluated_at"].replace("Z", "+00:00"))
    def times(value):
        if isinstance(value, dict): return {k: times(v) for k,v in value.items()}
        if isinstance(value, list): return [times(v) for v in value]
        if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}T", value):
            return (datetime.fromisoformat(value.replace("Z", "+00:00"))+offset).isoformat().replace("+00:00", "Z")
        return value
    m, r = times(m), times(r)
    identity = collection_request().business_identity.model_dump(mode="json")
    identity.update(site_url=m["business"]["site_url"], normalized_domain="example.test",
        public_gbp_url=r["public_input"]["evidence_input"]["context"]["customer_public_gbp"]["public_gbp_url"],
        primary_location=r["public_input"]["evidence_input"]["context"]["target_market"])
    r["public_input"]["business_identity"] = identity
    sources = r["public_input"]["evidence_input"]["sources"]
    market = next(s for s in sources if s["kind"] == "serp")
    for s in sources:
        if s["kind"] == "competitor": s["payload"]["market_snapshot_checksum"] = request_digest(market["payload"])
        if s["kind"] == "public_gbp": s["payload"]["subject_reference_checksum"] = request_digest(r["public_input"]["evidence_input"]["context"]["customer_public_gbp"])
        s["binding"]["payload_checksum"] = request_digest(s["payload"])
    for s in r["first_party_input"]["snapshots"]:
        s["payload_checksum"] = request_digest(s["normalized_payload"])
    for s in m["snapshots"]:
        public = next((x for x in sources if x["binding"]["snapshot_id"] == s["snapshot_id"]), None)
        private = next((x for x in r["first_party_input"]["snapshots"] if x["snapshot_id"] == s["snapshot_id"]), None)
        s["payload_checksum"] = public["binding"]["payload_checksum"] if public else private["payload_checksum"]
    m["generation_input_checksum"] = verified_request_digest(r)
    m["input_checksum"] = verified_request_digest({k:v for k,v in m.items() if k != "input_checksum"})
    return AnalysisInputManifest.model_validate_json(canonical_json_bytes(m)), CapabilityServiceInput.model_validate_json(canonical_json_bytes(r))


async def main():
    command = json.loads(sys.stdin.readline())
    manifest, request = seed(command["clean"])
    # Preserve sealed Python JSON numbers through the JS test controller. In
    # particular, 1.0 must not silently become 1 before checksum verification.
    emit(dict(kind="seed", manifest=manifest.model_dump(mode="json"), request=request.model_dump(mode="json"),
        private_payloads={str(s.snapshot_id):canonical_json_bytes(s.normalized_payload).decode()
            for s in request.first_party_input.snapshots}))
    if json.loads(sys.stdin.readline()) != {"kind":"ready"}: raise ValueError("Parent not ready")
    calls = []
    async def transport(sent):
        rpc = sent.url.path.split("/")[-1]
        if sent.url.host != "local-test.invalid": raise ValueError("External request blocked")
        calls.append(rpc)
        emit(dict(kind="rpc", rpc=rpc, args=json.loads(sent.content)))
        reply = json.loads(sys.stdin.readline())
        if "body_text" in reply:
            return httpx.Response(reply["status"], content=reply["body_text"], headers={"content-type":"application/json"})
        return httpx.Response(reply["status"], json=reply["body"])

    redis = FakeRedis()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            repository = SupabaseCapabilityInputRepository(url="https://local-test.invalid",service_role_key="local-only",http_client=client)
            persister = SupabaseCapabilityResultPersister(url="https://local-test.invalid",service_role_key="local-only",http_client=client)
            await repository.prepare(manifest=manifest, request=request, run_generation=1)
            checkpoints = JobCheckpoints(redis,prefix="test:delivery",ttl_seconds=300,run_generation=1)
            await redis.set(checkpoints.keys.state(manifest.job_id),json.dumps(dict(run_generation=1,status="running")))
            executor = CapabilityV22Executor(resolver=repository,persister=persister)
            task = dict(schema_version="v22_capability_request_envelope_v1", capability_request=dict(
                case_id=str(manifest.case_id),manifest_id=str(manifest.id),input_checksum=manifest.input_checksum,report_type="verified_execution"))
            args = dict(job_id=manifest.job_id,request=task,submitted_at=datetime.now(timezone.utc),checkpoints=checkpoints)
            lost = False
            try:
                report = await executor.execute(**args)
            except TransientJobError:
                if not command.get("lose_completion"): raise
                lost = True
                report = await executor.execute(**args)
            replay = await executor.execute(**args)
            assert report == replay
            emit(dict(kind="result",report=report.model_dump(mode="json"),calls=calls,recovered_completion=lost))
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
