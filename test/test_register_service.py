import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from services import register_service as register_service_module
from services.register import mail_provider


class FakeAccountService:
    def list_accounts(self) -> list[dict]:
        return []


class RegisterServiceSchedulerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
