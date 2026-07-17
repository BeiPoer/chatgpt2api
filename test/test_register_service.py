import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from services import register_service as register_service_module
from services.openai_backend_api import OpenAIBackendAPI
from services.register import mail_provider


class FakeAccountService:
    def list_accounts(self) -> list[dict]:
        return []


class RegisterServiceSchedulerTests(unittest.TestCase):
    def test_normalize_defaults_cf_block_sleep(self) -> None:
        cfg = register_service_module._normalize({})
        self.assertEqual(cfg["cf_block_sleep"], 3.0)

        cfg = register_service_module._normalize({"cf_block_sleep": "5.5"})
        self.assertEqual(cfg["cf_block_sleep"], 5.5)

        cfg = register_service_module._normalize({"cf_block_sleep": -1})
        self.assertEqual(cfg["cf_block_sleep"], 0.0)

    def test_available_mode_releases_stale_worker_slot_and_submits_next_task(self) -> None:
        calls: list[int] = []
        second_call = threading.Event()

        def slow_worker(index: int) -> dict:
            calls.append(index)
            if len(calls) >= 2:
                second_call.set()
            time.sleep(0.2)
            return {"ok": False, "index": index}

        old_sink = register_service_module.openai_register.register_log_sink
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = register_service_module.RegisterService(Path(tmp_dir) / "register.json")
            try:
                service.update({
                    "mode": "available",
                    "target_available": 1,
                    "check_interval": 1,
                    "threads": 1,
                })
                with mock.patch.object(register_service_module, "account_service", FakeAccountService()), mock.patch.object(
                    register_service_module.openai_register,
                    "worker",
                    side_effect=slow_worker,
                ), mock.patch.object(
                    register_service_module,
                    "SCHEDULER_WAIT_SECONDS",
                    0.01,
                ), mock.patch.object(
                    register_service_module.RegisterService,
                    "_worker_timeout_seconds",
                    return_value=0.03,
                ):
                    service.start()
                    self.assertTrue(second_call.wait(timeout=1.0))
                    service.stop()
                    self.assertIsNotNone(service._runner)
                    service._runner.join(timeout=2.0)
            finally:
                register_service_module.openai_register.register_log_sink = old_sink

        self.assertGreaterEqual(len(calls), 2)
        log_text = "\n".join(item["text"] for item in service.get()["logs"])
        self.assertIn("已释放 1 个调度槽继续补新任务", log_text)

    def test_stats_save_error_does_not_raise_or_stop_scheduler(self) -> None:
        old_sink = register_service_module.openai_register.register_log_sink
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = register_service_module.RegisterService(Path(tmp_dir) / "register.json")
            try:
                with mock.patch.object(type(service._store_file), "write_text", side_effect=OSError(24, "Too many open files")):
                    service._bump(force_save=True, done=1)
            finally:
                register_service_module.openai_register.register_log_sink = old_sink

        self.assertEqual(service.get()["stats"]["done"], 1)
        log_text = "\n".join(item["text"] for item in service.get()["logs"])
        self.assertIn("保存注册状态失败", log_text)


class OutlookTokenProviderTests(unittest.TestCase):
    def test_imap_uses_configured_request_timeout(self) -> None:
        calls: list[dict] = []

        class FakeIMAP:
            def __init__(self, host: str, timeout: float | None = None):
                calls.append({"host": host, "timeout": timeout})

            def authenticate(self, _mechanism, _handler):
                return "OK", []

            def select(self, _mailbox, readonly=False):
                return "OK", []

            def uid(self, command, *args):
                if command == "search":
                    return "OK", [b""]
                return "OK", []

            def logout(self):
                return "OK", []

        provider = mail_provider.OutlookTokenProvider(
            {
                "provider_ref": "outlook_token#1",
                "mailboxes": "user@example.com----pass----client----refresh",
                "imap_host": "imap.example.com",
                "mode": "imap",
            },
            {"request_timeout": 12.5, "wait_timeout": 1.0, "wait_interval": 0.2, "user_agent": "test"},
        )

        with mock.patch.object(mail_provider.imaplib, "IMAP4_SSL", FakeIMAP):
            self.assertEqual(provider._imap_messages({"address": "user@example.com"}, "access-token"), [])

        self.assertEqual(calls, [{"host": "imap.example.com", "timeout": 12.5}])


class OpenAIBackendUserInfoTests(unittest.TestCase):
    def test_get_user_info_waits_for_sibling_requests_after_failure(self) -> None:
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "access-token"
        completed: list[str] = []

        def fail_fast():
            raise RuntimeError("first request failed")

        def finish_later(name: str):
            time.sleep(0.03)
            completed.append(name)
            return {}

        backend._get_me = fail_fast
        backend._get_conversation_init = lambda: finish_later("init")
        backend._get_default_account = lambda: finish_later("account")

        with self.assertRaisesRegex(RuntimeError, "first request failed"):
            backend.get_user_info()

        self.assertCountEqual(completed, ["init", "account"])


if __name__ == "__main__":
    unittest.main()
