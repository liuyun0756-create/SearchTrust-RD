import logging

from app.security_v22.logging import REDACTED, digest_suffix, safe_error_type, safe_text


def test_safe_logging_redacts_tokens_and_oauth_query_values() -> None:
    sentinel = "PRIVATE_SECURITY_SENTINEL"
    value = safe_text(
        f"Bearer {sentinel} ya29.{sentinel} 1//{sentinel} "
        f"private@example.test https://example.test/callback?code={sentinel}&state={sentinel}"
    )
    assert sentinel not in value
    assert value.count(REDACTED) >= 4
    assert "private@example.test" not in value
    assert "example.test" not in value


def test_safe_logging_uses_digest_not_customer_identifier(caplog) -> None:
    customer_domain = "private-customer.example"
    with caplog.at_level(logging.INFO):
        logging.getLogger("security-test").info(
            "operation domain_digest=%s", digest_suffix(customer_domain)
        )
    assert customer_domain not in caplog.text
    assert digest_suffix(customer_domain) in caplog.text
    assert safe_error_type(RuntimeError("Bearer private")) == "RuntimeError"


def test_safe_text_is_bounded() -> None:
    assert len(safe_text("x" * 10_000)) == 2_000
