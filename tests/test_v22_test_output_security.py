import os
import sys

import pytest

from support.output_security import redact_sensitive_output, run_diagnostic_command


pytestmark = pytest.mark.contract


def test_diagnostics_redact_headers_oauth_parameters_and_configured_secrets() -> None:
    planted = "PLANTED-SECRET-8f3c27b1"
    rendered = redact_sensitive_output(
        "Authorization: Bearer provider-token-123456\n"
        "https://example.test/callback?code_verifier=oauth-verifier-123456\n"
        f"configured={planted}\n",
        secret_values=[planted],
    )

    assert planted not in rendered
    assert "provider-token-123456" not in rendered
    assert "oauth-verifier-123456" not in rendered
    assert rendered.count("[REDACTED]") == 3


def test_safe_diagnostics_are_unchanged() -> None:
    output = "1625 passed, 1 deselected in 27.01s\n"
    assert redact_sensitive_output(output) == output


def test_subprocess_diagnostics_are_redacted_and_not_retained(
    capsys: pytest.CaptureFixture[str], tmp_path,
) -> None:
    planted = "PLANTED-SUBPROCESS-SECRET-17c9"
    environment = {**os.environ, "PLANTED_API_KEY": planted}

    result = run_diagnostic_command(
        [sys.executable, "-c", "import os; print(os.environ['PLANTED_API_KEY'])"],
        cwd=tmp_path,
        environment=environment,
    )

    captured = capsys.readouterr()
    assert result == 0
    assert planted not in captured.out + captured.err
    assert "[REDACTED]" in captured.out
    assert list(tmp_path.iterdir()) == []
