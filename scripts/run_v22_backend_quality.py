from pathlib import Path
from collections.abc import Sequence
import argparse
import os
import sys


ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"
for import_root in (ROOT, TESTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from support.output_security import run_diagnostic_command  # noqa: E402


def fast_test_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-m",
        "not redis_integration",
        "--disable-socket",
        "--allow-unix-socket",
    ]


def run_fast() -> int:
    environment = dict(os.environ)
    environment["SEARCHTRUST_TESTING"] = "1"
    return run_diagnostic_command(
        fast_test_command(),
        cwd=ROOT,
        environment=environment,
    )


def run_redis() -> int:
    return run_diagnostic_command(
        [sys.executable, str(ROOT / "scripts" / "run_v22_redis_integration.py")],
        cwd=ROOT,
        environment={**os.environ, "SEARCHTRUST_TESTING": "1"},
    )


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SearchTrust V2.2 backend gate")
    parser.add_argument("mode", choices=("fast", "release"))
    selected = parser.parse_args(arguments)

    fast_result = run_fast()
    if selected.mode == "fast" or fast_result != 0:
        return fast_result
    return run_redis()


if __name__ == "__main__":
    raise SystemExit(main())
