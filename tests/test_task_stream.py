import unittest

from app.core.config import Settings
from app.core.task_store import SSE_HEARTBEAT, delete_state, set_state, subscribe


class TaskStreamTests(unittest.IsolatedAsyncioTestCase):
    def test_long_report_timeout_defaults(self):
        fields = Settings.model_fields
        self.assertEqual(fields["DIFY_STREAM_TIMEOUT"].default, 1200)
        self.assertEqual(fields["TASK_STREAM_TIMEOUT"].default, 1260)
        self.assertEqual(fields["TASK_STREAM_HEARTBEAT_INTERVAL"].default, 20)

    async def test_quiet_task_emits_heartbeat_before_stream_timeout(self):
        task_id = "task-stream-heartbeat"
        set_state(task_id, {"status": "reporting", "progress": {"percent": 85}})
        stream = subscribe(task_id, timeout=0.08, heartbeat_interval=0.01)

        try:
            first = await anext(stream)
            heartbeat = await anext(stream)
        finally:
            await stream.aclose()
            delete_state(task_id)

        self.assertEqual(first["status"], "reporting")
        self.assertIs(heartbeat, SSE_HEARTBEAT)
