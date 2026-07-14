import unittest
from unittest.mock import AsyncMock, patch

from app.tasks.dify_client import call_dify_workflow


class RetryableOutputError(RuntimeError):
    retryable = True


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
