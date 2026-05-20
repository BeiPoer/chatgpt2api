from __future__ import annotations

import json
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from services.image_task_service import ImageTaskService
from services.storage.json_storage import JSONStorageBackend
from services.auth_service import AuthService


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}


def wait_for_task(service: ImageTaskService, identity: dict[str, object], task_id: str, status: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = service.list_tasks(identity, [task_id])
        last = (result.get("items") or [None])[0]
        if last and last.get("status") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {status}, last={last}")


class ImageTaskServiceTests(unittest.TestCase):
    def make_service(self, path: Path, handler=None) -> ImageTaskService:
        return ImageTaskService(
            path,
            generation_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/image.png"}]}),
            edit_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/edit.png"}]}),
            retention_days_getter=lambda: 30,
        )

    def test_duplicate_submit_uses_existing_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls = 0

            def handler(_payload):
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            first = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            second = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            self.assertEqual(first["id"], "task-1")
            self.assertEqual(second["id"], "task-1")
            task = wait_for_task(service, OWNER, "task-1", "success")
            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")
            self.assertEqual(calls, 1)

    def test_different_owner_cannot_query_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            service.submit_generation(
                OWNER,
                client_task_id="private-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            wait_for_task(service, OWNER, "private-task", "success")
            result = service.list_tasks(OTHER_OWNER, ["private-task"])

            self.assertEqual(result["items"], [])
            self.assertEqual(result["missing_ids"], ["private-task"])

    def test_success_task_persists_to_new_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            service.submit_generation(
                OWNER,
                client_task_id="persisted-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OWNER, "persisted-task", "success")

            reloaded = self.make_service(path)
            result = reloaded.list_tasks(OWNER, ["persisted-task"])

            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(result["items"][0]["status"], "success")
            self.assertEqual(result["items"][0]["data"][0]["url"], "http://example.test/image.png")

    def test_startup_marks_unfinished_tasks_as_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "queued-task",
                                "owner_id": "owner-1",
                                "status": "queued",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                            {
                                "id": "running-task",
                                "owner_id": "owner-1",
                                "status": "running",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            service = self.make_service(path)
            result = service.list_tasks(OWNER, ["queued-task", "running-task"])

            self.assertEqual([item["status"] for item in result["items"]], ["error", "error"])
            self.assertTrue(all("已中断" in item.get("error", "") for item in result["items"]))

    def test_startup_refunds_reserved_quota_for_unfinished_user_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, _ = auth_service.create_key(role="user", name="Alice", quota=1)
            auth_service.consume_quota({"id": item["id"], "role": "user"})
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "running-task",
                                "owner_id": item["id"],
                                "status": "running",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                                "quota_reserved": True,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch("services.image_task_service.auth_service", auth_service):
                self.make_service(path)

            updated = auth_service.list_keys(role="user")[0]
            self.assertEqual(updated["used_quota"], 0)
            self.assertEqual(updated["remaining_quota"], 1)

    def test_submit_generation_consumes_user_key_quota(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = auth_service.create_key(role="user", name="Alice", quota=1)
            identity = auth_service.authenticate(raw_key)
            self.assertIsNotNone(identity)

            with mock.patch("services.image_task_service.auth_service", auth_service):
                service = self.make_service(Path(tmp_dir) / "image_tasks.json")
                service.submit_generation(
                    identity,
                    client_task_id="quota-task",
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                    base_url="http://local.test",
                )
                wait_for_task(service, identity, "quota-task", "success")

            updated = auth_service.list_keys(role="user")[0]
            self.assertEqual(updated["id"], item["id"])
            self.assertEqual(updated["used_quota"], 1)
            self.assertEqual(updated["remaining_quota"], 0)

    def test_failed_generation_refunds_reserved_quota(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            auth_service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            _, raw_key = auth_service.create_key(role="user", name="Alice", quota=1)
            identity = auth_service.authenticate(raw_key)
            self.assertIsNotNone(identity)

            with mock.patch("services.image_task_service.auth_service", auth_service):
                service = self.make_service(
                    Path(tmp_dir) / "image_tasks.json",
                    handler=lambda _payload: (_ for _ in ()).throw(RuntimeError("boom")),
                )
                service.submit_generation(
                    identity,
                    client_task_id="failed-task",
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                    base_url="http://local.test",
                )
                wait_for_task(service, identity, "failed-task", "error")

            updated = auth_service.list_keys(role="user")[0]
            self.assertEqual(updated["used_quota"], 0)
            self.assertEqual(updated["remaining_quota"], 1)


if __name__ == "__main__":
    unittest.main()
