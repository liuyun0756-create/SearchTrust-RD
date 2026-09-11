"""Test-only ARQ worker with a deterministic interruption checkpoint."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic
from uuid import UUID

from arq.connections import ArqRedis, RedisSettings
from arq.worker import func

from app.jobs_v22.checkpoints import JobCheckpoints
from app.jobs_v22.queue import JobQueue
from app.jobs_v22.store import DurableJobStore
from app.jobs_v22.models import utc_now
from app.report_v22.models import ReportV22
from support.network_guard import validate_redis_test_environment


ROOT = Path(__file__).resolve().parents[3]
CHECKPOINT_NAME = "probe-provider-result-v1"
_FALLBACK_URL = "redis://127.0.0.1:1/15"
_FALLBACK_PREFIX = "searchtrust:v22:test:00000000000000000000000000000000:"


def probe_queue_name(prefix: str) -> str:
    return f"{prefix}probe-queue"


def checkpoint_signal_key(prefix: str, job_id: UUID, generation: int) -> str:
    return f"{prefix}probe:checkpoint:{job_id}:{generation}"


def resumed_signal_key(prefix: str, job_id: UUID, generation: int) -> str:
    return f"{prefix}probe:resumed:{job_id}:{generation}"


def probe_report() -> ReportV22:
    return ReportV22.model_validate_json(
        (ROOT / "contracts" / "v2.2" / "fixtures" / "prospect.json").read_text(
            encoding="utf-8"
        )
    )


class ProbeQueue(JobQueue):
    def __init__(self, pool: ArqRedis, *, prefix: str) -> None:
        self.pool = pool
        self.prefix = prefix

    async def enqueue(self, job_id: UUID, run_generation: int) -> bool:
        job = await self.pool.enqueue_job(
            "execute_v22_job",
            str(job_id),
            run_generation,
            _job_id=f"{self.prefix}probe:{job_id}:run:{run_generation}",
            _queue_name=probe_queue_name(self.prefix),
            _expires=120,
        )
        return job is not None


async def execute_probe_job(ctx: dict, job_id_value: str, run_generation: int) -> None:
    job_id = UUID(job_id_value)
    store: DurableJobStore = ctx["store"]
    state = await store.require_state(job_id)
    if state.terminal or state.run_generation != run_generation:
        return

    token = f"probe-owner-{run_generation}"
    if not await store.acquire_lease(
        job_id,
        generation=run_generation,
        token=token,
        ttl_seconds=60,
    ):
        return

    running = await store.transition(
        job_id,
        status="running",
        stage="collecting_site",
        progress=max(state.progress, 1),
        message="Probe worker is running.",
        now=utc_now(),
        attempt_count=state.attempt_count + 1,
        expected_generation=run_generation,
    )
    checkpoints = JobCheckpoints(
        ctx["redis"],
        prefix=store.keys.prefix,
        ttl_seconds=120,
        run_generation=run_generation,
    )
    existing = await checkpoints.get(job_id, CHECKPOINT_NAME)
    if existing is None:
        saved = await checkpoints.save(
            job_id,
            CHECKPOINT_NAME,
            {"job_id": str(job_id), "created_by_generation": run_generation},
        )
        if not saved:
            raise RuntimeError("probe checkpoint was not persisted")
        await ctx["redis"].set(
            checkpoint_signal_key(store.keys.prefix + ":", job_id, run_generation),
            b"ready",
            ex=120,
        )
        await asyncio.Event().wait()
        return

    completed = await store.transition(
        job_id,
        status="succeeded",
        stage="completed",
        progress=100,
        message="Probe recovery complete.",
        now=utc_now(),
        report=probe_report(),
        expected_generation=run_generation,
    )
    if completed.applied:
        await ctx["redis"].set(
            resumed_signal_key(store.keys.prefix + ":", job_id, run_generation),
            b"ready",
            ex=120,
        )
    await store.release_lease(job_id, generation=run_generation, token=token)


async def probe_startup(ctx: dict) -> None:
    validate_redis_test_environment(os.environ)
    prefix = os.environ["V22_TEST_REDIS_PREFIX"].rstrip(":")
    ctx["store"] = DurableJobStore(
        ctx["redis"],
        prefix=prefix,
        state_ttl_seconds=120,
        job_timeout_seconds=1200,
    )


class ProbeWorkerSettings:
    functions = [func(execute_probe_job, name="execute_v22_job", max_tries=1, keep_result=0)]
    redis_settings = RedisSettings.from_dsn(os.environ.get("V22_TEST_REDIS_URL", _FALLBACK_URL))
    queue_name = probe_queue_name(os.environ.get("V22_TEST_REDIS_PREFIX", _FALLBACK_PREFIX))
    on_startup = probe_startup
    max_jobs = 1
    job_timeout = 120
    max_tries = 1
    keep_result = 0
    retry_jobs = False
    poll_delay = 0.05
    health_check_interval = 1
    health_check_key = f"{os.environ.get('V22_TEST_REDIS_PREFIX', _FALLBACK_PREFIX)}probe-health"
    job_completion_wait = 0


class ProbeWorkerProcess:
    """Own one worker child and guarantee bounded termination and reaping."""

    def __init__(self, environment: Mapping[str, str]) -> None:
        self.environment = dict(environment)
        self.process: subprocess.Popen[bytes] | None = None
        self._output = tempfile.TemporaryFile()

    def __enter__(self) -> "ProbeWorkerProcess":
        environment = {
            **self.environment,
            "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "tests"))),
            "PYTHONUNBUFFERED": "1",
            "SEARCHTRUST_TESTING": "1",
        }
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "arq",
                "tests.integration.support.worker_probe.ProbeWorkerSettings",
            ],
            cwd=ROOT,
            env=environment,
            stdout=self._output,
            stderr=subprocess.STDOUT,
        )
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def stop(self, *, timeout: float = 8) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=timeout)
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("probe worker process was not reaped")
        self._output.close()

    def diagnostics(self) -> str:
        self._output.flush()
        self._output.seek(0)
        return self._output.read().decode("utf-8", errors="replace")[-4000:]


async def wait_for_signal(
    redis,
    key: str,
    worker: ProbeWorkerProcess,
    *,
    timeout: float = 12,
) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if await redis.get(key) == b"ready":
            return
        if worker.process is not None and worker.process.poll() is not None:
            raise RuntimeError(f"probe worker exited early\n{worker.diagnostics()}")
        await asyncio.sleep(0.05)
    raise TimeoutError(f"probe worker did not signal {key}\n{worker.diagnostics()}")
