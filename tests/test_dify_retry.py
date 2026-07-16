import unittest
from unittest.mock import AsyncMock, patch

from app.tasks.dify_client import call_dify_workflow


class RetryableOutputError(RuntimeError):
    retryable = True


class RetryableValidationError(RuntimeError):
    retryable = True
    error_code = "V21_OUTPUT_INVALID"
    validation_errors = ["specific validation failure"]


class DifyOutputRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retryable_native_output_restarts_the_workflow(self):
        attempts = 0

        def validate_output(_output):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RetryableOutputError("evidence gate rejected output")

        with patch("app.tasks.dify_client._stream_workflow", new_callable=AsyncMock) as stream, patch(
            "app.tasks.dify_client.asyncio.sleep", new_callable=AsyncMock
        ):
            stream.return_value = {"report_v2_1": {"schema_version": "2.1"}}
            result = await call_dify_workflow(
                url="https://example.com/service",
                page_type="Service Page",
                language="English",
                content="Evidence-backed page content.",
                gbp_data={},
                task_id="retry-fixture",
                output_validator=validate_output,
            )

        self.assertEqual(result["report_v2_1"]["schema_version"], "2.1")
        self.assertEqual(attempts, 2)
        self.assertEqual(stream.await_count, 2)

    async def test_dify_inputs_force_english_and_include_backend_fact_sources(self):
        with patch("app.tasks.dify_client._stream_workflow", new_callable=AsyncMock) as stream:
            stream.return_value = {"ok": True}
            await call_dify_workflow(
                url="https://example.com/service",
                page_type="Service Page",
                language="中文",
                content="Checked page content.",
                gbp_data={"name": "Example"},
                task_id="english-input-fixture",
                page_facts={"business_names": ["Example"]},
                evidence_ledger='[{"id":"page-0001","text":"Checked page content."}]',
            )

        inputs = stream.await_args.args[0]
        self.assertEqual(inputs["language"], "English")
        self.assertIn("business_names", inputs["page_facts"])
        self.assertIn("page-0001", inputs["evidence_ledger"])

    async def test_retry_exhaustion_preserves_validation_errors(self):
        def reject(_output):
            raise RetryableValidationError("invalid output")

        with patch("app.tasks.dify_client._stream_workflow", new_callable=AsyncMock) as stream, patch(
            "app.tasks.dify_client.asyncio.sleep", new_callable=AsyncMock
        ):
            stream.return_value = {"ok": True}
            with self.assertRaises(RetryableValidationError) as raised:
                await call_dify_workflow(
                    url="https://example.com/service",
                    page_type="Service Page",
                    language="English",
                    content="Checked page content.",
                    gbp_data={},
                    task_id="validation-details-fixture",
                    output_validator=reject,
                )

        self.assertEqual(raised.exception.validation_errors, ["specific validation failure"])
        self.assertEqual(stream.await_count, 3)
