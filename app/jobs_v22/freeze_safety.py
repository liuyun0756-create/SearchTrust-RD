"""Small durable freeze intents; uncertainty never authorizes another write."""
from datetime import datetime
import json

from app.jobs_v22.errors import DeterministicJobError, TransientJobError, JobLeaseLost

DEFAULT_FREEZE_MAX_INPUT_BYTES = 40_000_000


def freeze_json_bytes(payload):
    # Identical to HTTPX's JSON encoder, including UTF-8 and escaping.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class FreezeRecoveryGuard:
    def __init__(self, *, rpc, redis, prefix, receipt, token, kind, ttl_seconds):
        self.rpc, self.redis, self.receipt = rpc, redis, receipt
        self.token, self.kind, self.ttl = token, kind, ttl_seconds
        self.key = f"{prefix}:freeze-intent:{kind}:{receipt.job_id}:{receipt.run_generation}"

    def unavailable(self):
        return TransientJobError("V22_CAPABILITY_FREEZE_RECOVERY_UNAVAILABLE",
            "The saved report task is awaiting a safe storage check.")

    async def observe(self):
        r = self.receipt
        value = await self.rpc.post("inspect_v22_capability_preparation", dict(
            p_user_id=str(r.user_id), p_case_id=str(r.case_id), p_job_id=str(r.job_id),
            p_run_generation=r.run_generation, p_lease_token=str(self.token), p_kind=self.kind))
        if (not isinstance(value, dict) or value.get("job_id") != str(r.job_id)
                or value.get("run_generation") != r.run_generation
                or type(value.get("manifest_exists")) is not bool
                or type(value.get("lease_owned")) is not bool):
            raise self.unavailable()
        try:
            boot = datetime.fromisoformat(value["database_started_at"].replace("Z", "+00:00"))
            if boot.utcoffset() is None:
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise self.unavailable() from None
        return value

    async def reconcile_pending(self):
        saved = await self.redis.get(self.key)
        if saved is None:
            return
        try:
            saved = json.loads(saved)
            if saved["version"] != 1:
                raise ValueError()
            old_boot = datetime.fromisoformat(saved["database_started_at"].replace("Z", "+00:00"))
            if old_boot.utcoffset() is None:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise self.unavailable() from None
        state = await self.observe()
        # A commit/terminal update always wins over a restart signal: no refund
        # based on an interrupted acknowledgement and no duplicate freeze.
        if state["manifest_exists"] or state.get("status") not in {"queued", "running"} or not state["lease_owned"]:
            raise JobLeaseLost()
        boot = datetime.fromisoformat(state["database_started_at"].replace("Z", "+00:00"))
        if boot != old_boot:
            raise DeterministicJobError("V22_CAPABILITY_FREEZE_DATABASE_RESTARTED",
                "Report storage restarted while preparing this task. Start a new analysis after review.")

    async def arm(self):
        await self.reconcile_pending()
        state = await self.observe()
        if state["manifest_exists"] or state.get("status") not in {"queued", "running"} or not state["lease_owned"]:
            raise JobLeaseLost()
        # NX retains the boot observed before the FIRST possibly committed call.
        # Redis failure prevents the call. Never delete this marker on transport
        # failure; a new worker must reconcile it before any provider work.
        saved = json.dumps(dict(version=1, database_started_at=state["database_started_at"]))
        await self.redis.set(self.key, saved, ex=self.ttl, nx=True)
        if await self.redis.get(self.key) is None:
            raise self.unavailable()
        await self.reconcile_pending()
