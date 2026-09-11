from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_v22_backend_quality as quality  # noqa: E402
import run_v22_redis_integration as redis_quality  # noqa: E402


pytestmark = pytest.mark.contract


def test_fast_command_disables_external_network_and_skips_real_redis() -> None:
    command = quality.fast_test_command()

    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert "not redis_integration" in command
    assert "--disable-socket" in command
    assert "--allow-unix-socket" in command
    assert not any(argument.startswith("--allow-hosts") for argument in command)


def test_fast_mode_runs_only_the_fast_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(quality, "run_fast", lambda: calls.append("fast") or 0)
    monkeypatch.setattr(quality, "run_redis", lambda: calls.append("redis") or 0)

    assert quality.main(["fast"]) == 0
    assert calls == ["fast"]


def test_release_mode_stops_before_redis_when_fast_gate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(quality, "run_fast", lambda: calls.append("fast") or 7)
    monkeypatch.setattr(quality, "run_redis", lambda: calls.append("redis") or 0)

    assert quality.main(["release"]) == 7
    assert calls == ["fast"]


def test_release_mode_runs_real_redis_after_fast_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(quality, "run_fast", lambda: calls.append("fast") or 0)
    monkeypatch.setattr(quality, "run_redis", lambda: calls.append("redis") or 0)

    assert quality.main(["release"]) == 0
    assert calls == ["fast", "redis"]


def test_redis_command_allows_only_explicit_loopback_hosts() -> None:
    command = redis_quality.redis_test_command()

    assert "redis_integration" in command
    assert "--disable-socket" in command
    assert "--allow-unix-socket" in command
    assert "--allow-hosts=127.0.0.1,localhost,::1" in command


def test_redis_cleanup_scope_rejects_unowned_compose_projects() -> None:
    with pytest.raises(ValueError, match="unsafe Docker Compose project name"):
        redis_quality._compose_command("production", ["down"])
