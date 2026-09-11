from collections.abc import Mapping, Sequence
from pathlib import Path
import os
import re
import subprocess
import tempfile


_SENSITIVE_ENV_NAME = re.compile(
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|AUTHORIZATION|PRIVATE_KEY)", re.IGNORECASE
)
_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access_token|refresh_token|client_secret|"
        r"code_verifier|share_token|share_secret|token)=)[^&\s]+"
    ),
    re.compile(
        r'(?i)("(?:api[_-]?key|access_token|refresh_token|client_secret|token)"\s*:\s*")'
        r'[^"\\]*(")'
    ),
    re.compile(r"\b(?:sk|rk|pk)_[A-Za-z0-9_-]{12,}\b"),
)


def configured_secret_values(environment: Mapping[str, str]) -> list[str]:
    return sorted(
        {
            value
            for name, value in environment.items()
            if _SENSITIVE_ENV_NAME.search(name) and len(value) >= 8
        },
        key=len,
        reverse=True,
    )


def redact_sensitive_output(
    output: str,
    *,
    secret_values: Sequence[str] = (),
) -> str:
    redacted = output
    for secret in sorted(set(secret_values), key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")

    redacted = _SENSITIVE_PATTERNS[0].sub(r"\1[REDACTED]", redacted)
    redacted = _SENSITIVE_PATTERNS[1].sub(r"\1[REDACTED]", redacted)
    redacted = _SENSITIVE_PATTERNS[2].sub(r"\1[REDACTED]\2", redacted)
    redacted = _SENSITIVE_PATTERNS[3].sub("[REDACTED]", redacted)
    return redacted


def run_diagnostic_command(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> int:
    process_environment = dict(os.environ if environment is None else environment)
    secrets = configured_secret_values(process_environment)
    with tempfile.TemporaryDirectory(prefix="searchtrust-v22-quality-") as temp_dir:
        diagnostics = Path(temp_dir) / "diagnostics.log"
        with diagnostics.open("w", encoding="utf-8") as output:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                env=process_environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        safe_output = redact_sensitive_output(
            diagnostics.read_text(encoding="utf-8", errors="replace"),
            secret_values=secrets,
        )
        if safe_output:
            print(safe_output, end="" if safe_output.endswith("\n") else "\n")
        return completed.returncode
