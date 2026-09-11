from pathlib import Path
from collections.abc import Mapping, Sequence
import os
import re
import subprocess
import sys
import uuid

from redis import Redis


ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"
for import_root in (ROOT, TESTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from integration.support.redis_process import (  # noqa: E402
    clear_prefixed_keys,
    generated_test_prefix,
    require_test_redis_environment,
)
from support.output_security import (  # noqa: E402
    configured_secret_values,
    redact_sensitive_output,
    run_diagnostic_command,
)


COMPOSE_FILE = ROOT / "docker-compose.test.yml"
_PROJECT_PATTERN = re.compile(r"^searchtrust-v22-tests-[a-z0-9-]+$")


def redis_test_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-m",
        "redis_integration",
        "--disable-socket",
        "--allow-unix-socket",
        "--allow-hosts=127.0.0.1,localhost,::1",
    ]


def _compose_command(project: str, arguments: Sequence[str]) -> list[str]:
    if not _PROJECT_PATTERN.fullmatch(project):
        raise ValueError("unsafe Docker Compose project name")
    return [
        "docker",
        "compose",
        "--project-name",
        project,
        "--file",
        str(COMPOSE_FILE),
        *arguments,
    ]


def _run_compose(
    project: str,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _compose_command(project, arguments),
        cwd=ROOT,
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
    )


def _safe_compose_failure(
    completed: subprocess.CompletedProcess[str],
    environment: Mapping[str, str],
) -> None:
    combined = f"{completed.stdout}\n{completed.stderr}".strip()
    safe = redact_sensitive_output(
        combined,
        secret_values=configured_secret_values(environment),
    )
    if safe:
        print(safe, file=sys.stderr)


def _port_from_compose(project: str, environment: Mapping[str, str]) -> int:
    completed = _run_compose(project, ["port", "redis", "6379"], environment=environment)
    if completed.returncode != 0:
        _safe_compose_failure(completed, environment)
        raise RuntimeError("could not resolve the isolated Redis port")
    endpoint = completed.stdout.strip().splitlines()[-1]
    port_text = endpoint.rsplit(":", 1)[-1]
    if not port_text.isdigit():
        raise RuntimeError("Docker Compose returned an invalid Redis port")
    return int(port_text)


def _run_against_environment(environment: Mapping[str, str]) -> int:
    redis_url, prefix = require_test_redis_environment(environment)
    client = Redis.from_url(redis_url, decode_responses=False)
    try:
        client.ping()
        clear_prefixed_keys(client, prefix)
        return run_diagnostic_command(
            redis_test_command(),
            cwd=ROOT,
            environment=environment,
        )
    finally:
        clear_prefixed_keys(client, prefix)
        client.close()


def main() -> int:
    base_environment = {**os.environ, "SEARCHTRUST_TESTING": "1"}
    if base_environment.get("V22_TEST_REDIS_URL"):
        context = "ci" if base_environment.get("GITHUB_ACTIONS") == "true" else "local"
        test_environment = {
            **base_environment,
            "V22_TEST_REDIS_PREFIX": generated_test_prefix(context=context),
        }
        return _run_against_environment(test_environment)

    project = f"searchtrust-v22-tests-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    started = _run_compose(
        project,
        ["up", "--detach", "--wait"],
        environment=base_environment,
    )
    if started.returncode != 0:
        _safe_compose_failure(started, base_environment)
        return started.returncode

    try:
        port = _port_from_compose(project, base_environment)
        test_environment = {
            **base_environment,
            "V22_TEST_REDIS_URL": f"redis://127.0.0.1:{port}/15",
            "V22_TEST_REDIS_PREFIX": generated_test_prefix(context="test"),
        }
        return _run_against_environment(test_environment)
    finally:
        stopped = _run_compose(
            project,
            ["down", "--volumes", "--remove-orphans"],
            environment=base_environment,
        )
        if stopped.returncode != 0:
            _safe_compose_failure(stopped, base_environment)


if __name__ == "__main__":
    raise SystemExit(main())
