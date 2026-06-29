from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.auth_service import AuthService
from services.image_task_service import ImageTaskService
from services.log_service import LoggedCall
from services.storage.json_storage import JSONStorageBackend


def _auth_service(tmp_dir: str, quota: int = 10):
    service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
    item, raw_key = service.create_key(role="user", name="Alice", quota=quota)
    identity = service.authenticate(raw_key)
    assert identity is not None
    return service, item, identity


class UserKeyQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_call_consumes_requested_image_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, _, identity = _auth_service(tmp_dir, quota=4)

            def handler(_payload):
                return {"data": [{"url": "https://example.test/1.png"}, {"url": "https://example.test/2.png"}]}

            with (
                mock.patch("services.log_service.auth_service", service),
                mock.patch("services.log_service.log_service.add", lambda *_args, **_kwargs: None),
            ):
                result = await LoggedCall(
                    identity,
                    "/v1/images/generations",
                    "gpt-image-2",
                    "image",
                    quota_amount=2,
                ).run(handler, {})

            self.assertEqual(len(result["data"]), 2)
            updated = service.list_keys(role="user")[0]
            self.assertEqual(updated["used_quota"], 2)
            self.assertEqual(updated["remaining_quota"], 2)

    async def test_image_call_refunds_missing_results_after_reserving_n(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, _, identity = _auth_service(tmp_dir, quota=4)

            def handler(_payload):
                return {"data": [{"url": "https://example.test/only-one.png"}]}

            with (
                mock.patch("services.log_service.auth_service", service),
                mock.patch("services.log_service.log_service.add", lambda *_args, **_kwargs: None),
            ):
                result = await LoggedCall(
                    identity,
                    "/v1/images/generations",
                    "gpt-image-2",
                    "image",
                    quota_amount=4,
                ).run(handler, {})

            self.assertEqual(len(result["data"]), 1)
            updated = service.list_keys(role="user")[0]
            self.assertEqual(updated["used_quota"], 1)
            self.assertEqual(updated["remaining_quota"], 3)

    async def test_image_call_checks_quota_against_requested_n(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, _, identity = _auth_service(tmp_dir, quota=3)

            with (
                mock.patch("services.log_service.auth_service", service),
                mock.patch("services.log_service.log_service.add", lambda *_args, **_kwargs: None),
            ):
                with self.assertRaises(HTTPException) as raised:
                    await LoggedCall(
                        identity,
                        "/v1/images/generations",
                        "gpt-image-2",
                        "image",
                        quota_amount=4,
                    ).run(lambda _payload: {"data": [{"url": "https://example.test/1.png"}]}, {})

            self.assertEqual(raised.exception.status_code, 429)
            updated = service.list_keys(role="user")[0]
            self.assertEqual(updated["used_quota"], 0)
            self.assertEqual(updated["remaining_quota"], 3)


class FakeResumeBackend:
    def _poll_image_results(self, _conversation_id, _timeout_secs):
        return ["file-one"], []

    def resolve_conversation_image_urls(self, _conversation_id, _file_ids, _sediment_ids, poll=False):
        return ["https://example.test/one.png"]

    def download_image_bytes(self, _urls):
        return [b"image-bytes"]

    def close(self):
        pass


class ImageTaskQuotaResumeTests(unittest.TestCase):
    def test_resume_poll_success_recharges_refunded_task_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service, item, identity = _auth_service(tmp_dir, quota=1)
            image_task_path = Path(tmp_dir) / "image_tasks.json"
            image_task_path.write_text(
                """{"tasks":[{"id":"task-1","owner_id":"%s","status":"error","mode":"generate","model":"gpt-image-2","created_at":"2099-01-01 00:00:00","updated_at":"2099-01-01 00:00:00","quota_reserved":true,"quota_refunded":true,"conversation_id":"conv-1","error":"poll \\u8d85\\u65f6"}]}"""
                % item["id"],
                encoding="utf-8",
            )

            with (
                mock.patch("services.image_task_service.auth_service", service),
                mock.patch("services.openai_backend_api.OpenAIBackendAPI", FakeResumeBackend),
                mock.patch("services.protocol.conversation.save_image_bytes", lambda _data, _base_url=None: "https://local.test/image.png"),
            ):
                task_service = ImageTaskService(image_task_path, retention_days_getter=lambda: 30)
                task_service.resume_poll(identity, "task-1", 5)
                deadline = time.time() + 2
                task = None
                while time.time() < deadline:
                    task = task_service.list_tasks(identity, ["task-1"])["items"][0]
                    if task["status"] == "success":
                        break
                    time.sleep(0.02)

            self.assertIsNotNone(task)
            self.assertEqual(task["status"], "success")
            updated = service.list_keys(role="user")[0]
            self.assertEqual(updated["used_quota"], 1)
            self.assertEqual(updated["remaining_quota"], 0)


if __name__ == "__main__":
    unittest.main()
