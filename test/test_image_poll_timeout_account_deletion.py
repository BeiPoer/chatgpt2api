from __future__ import annotations

import unittest
from unittest import mock

from services.openai_backend_api import ImagePollTimeoutError
from services.protocol import conversation


class FakeAccountService:
    def __init__(self) -> None:
        self.accounts = {
            "timeout-token": {"access_token": "timeout-token", "email": "timeout@example.test"},
            "success-token": {"access_token": "success-token", "email": "success@example.test"},
        }
        self.deleted: list[str] = []
        self.released: list[str] = []
        self.marked: list[tuple[str, bool]] = []

    def get_available_access_token(self, *args, **kwargs) -> str:
        for token in ("timeout-token", "success-token"):
            if token in self.accounts:
                return token
        raise RuntimeError("no available image quota")

    def get_account(self, access_token: str) -> dict | None:
        account = self.accounts.get(access_token)
        return dict(account) if account else None

    def release_image_slot(self, access_token: str) -> None:
        self.released.append(access_token)

    def delete_accounts(self, tokens: list[str]) -> dict:
        removed = 0
        for token in tokens:
            if token in self.accounts:
                self.deleted.append(token)
                self.accounts.pop(token, None)
                removed += 1
        return {"removed": removed, "items": list(self.accounts.values())}

    def mark_image_result(self, access_token: str, success: bool) -> dict | None:
        self.marked.append((access_token, success))
        return self.get_account(access_token)


class FakeBackend:
    def __init__(self, access_token: str = "") -> None:
        self.access_token = access_token
        self.progress_callback = None
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeFallbackBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__("timeout-token")
        self.retry_polls = 0

    def resolve_conversation_image_urls(self, *args, **kwargs) -> list[str]:
        return []

    def _poll_image_results(self, *args, **kwargs):
        self.retry_polls += 1
        raise ImagePollTimeoutError("ChatGPT 生图超时（已等待 1 秒）。")


class ImagePollTimeoutAccountDeletionTests(unittest.TestCase):
    def test_http2_stream_error_is_connection_error(self) -> None:
        self.assertTrue(conversation.is_tls_connection_error(
            "Failed to perform, curl: (92) HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR"
        ))

    def test_poll_timeout_deletes_account_and_retries_after_progress(self) -> None:
        account_service = FakeAccountService()

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            if backend.access_token == "timeout-token":
                yield conversation.ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    text="upstream started",
                    conversation_id="conv-timeout",
                )
                exc = ImagePollTimeoutError("ChatGPT 生图超时（已等待 1 秒）。")
                setattr(exc, "conversation_id", "conv-timeout")
                raise exc
            yield conversation.ImageOutput(
                kind="result",
                model=request.model,
                index=index,
                total=total,
                data=[{"url": "http://example.test/image.png", "revised_prompt": request.prompt}],
            )

        with (
            mock.patch.object(conversation, "account_service", account_service),
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs),
        ):
            outputs = conversation._generate_single_image(
                conversation.ConversationRequest(prompt="cat", model="gpt-image-2"),
                1,
                1,
            )

        self.assertEqual(account_service.deleted, ["timeout-token"])
        self.assertEqual(account_service.released, ["timeout-token"])
        self.assertNotIn("timeout-token", account_service.accounts)
        self.assertNotIn(("timeout-token", False), account_service.marked)
        self.assertIn(("success-token", True), account_service.marked)
        self.assertEqual([output.kind for output in outputs], ["result"])
        self.assertEqual(outputs[0].account_email, "success@example.test")

    def test_poll_timeout_retry_can_be_disabled(self) -> None:
        account_service = FakeAccountService()

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            yield conversation.ImageOutput(
                kind="progress",
                model=request.model,
                index=index,
                total=total,
                text="upstream started",
                conversation_id="conv-timeout",
            )
            exc = ImagePollTimeoutError("ChatGPT 生图超时（已等待 1 秒）。")
            setattr(exc, "conversation_id", "conv-timeout")
            raise exc

        with (
            mock.patch.object(conversation, "account_service", account_service),
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs),
            mock.patch.dict(conversation.config.data, {"image_poll_timeout_retry_enabled": False}),
        ):
            with self.assertRaises(conversation.ImageGenerationError) as caught:
                conversation._generate_single_image(
                    conversation.ConversationRequest(prompt="cat", model="gpt-image-2"),
                    1,
                    1,
                )

        self.assertEqual(account_service.deleted, [])
        self.assertEqual(account_service.marked, [("timeout-token", False)])
        self.assertIn("超时", str(caught.exception))
        self.assertEqual(caught.exception.conversation_id, "conv-timeout")

    def test_disabled_retry_skips_fallback_retry_poll(self) -> None:
        backend = FakeFallbackBackend()

        def fake_conversation_events(*args, **kwargs):
            yield {
                "type": "conversation.done",
                "conversation_id": "conv-timeout",
                "file_ids": [],
                "sediment_ids": [],
                "text": "",
                "turn_use_case": "image gen",
            }

        with (
            mock.patch.object(conversation, "conversation_events", fake_conversation_events),
            mock.patch.dict(conversation.config.data, {"image_poll_timeout_retry_enabled": False}),
        ):
            outputs = list(conversation.stream_image_outputs(
                backend,
                conversation.ConversationRequest(prompt="cat", model="gpt-image-2"),
            ))

        self.assertEqual(backend.retry_polls, 0)
        self.assertEqual([output.kind for output in outputs], ["message"])

    def test_generation_retry_stops_at_image_timeout_budget(self) -> None:
        account_service = FakeAccountService()
        calls = 0

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            nonlocal calls
            calls += 1
            raise RuntimeError(
                "Failed to perform, curl: (92) HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR"
            )

        with (
            mock.patch.object(conversation, "account_service", account_service),
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs),
            mock.patch.dict(conversation.config.data, {"image_poll_timeout_secs": 1, "image_poll_timeout_retry_enabled": True}),
            mock.patch.object(conversation.time, "monotonic", side_effect=[0.0, 0.1, 2.0]),
            mock.patch.object(conversation.time, "sleep"),
        ):
            with self.assertRaises(conversation.ImageGenerationError) as caught:
                conversation._generate_single_image(
                    conversation.ConversationRequest(prompt="cat", model="gpt-image-2"),
                    1,
                    1,
                )

        self.assertEqual(calls, 1)
        self.assertEqual(str(caught.exception), "image generation timed out")

    def test_generation_progress_stream_stops_at_image_timeout_budget(self) -> None:
        account_service = FakeAccountService()

        def fake_stream_image_outputs(backend, request, index=1, total=1):
            yield conversation.ImageOutput(
                kind="progress",
                model=request.model,
                index=index,
                total=total,
                text="still working",
                conversation_id="conv-timeout",
            )
            yield conversation.ImageOutput(
                kind="progress",
                model=request.model,
                index=index,
                total=total,
                text="still working",
                conversation_id="conv-timeout",
            )

        with (
            mock.patch.object(conversation, "account_service", account_service),
            mock.patch.object(conversation, "OpenAIBackendAPI", FakeBackend),
            mock.patch.object(conversation, "stream_image_outputs", fake_stream_image_outputs),
            mock.patch.dict(conversation.config.data, {"image_poll_timeout_secs": 1}),
            mock.patch.object(conversation.time, "monotonic", side_effect=[0.0, 0.1, 0.2, 2.0]),
        ):
            with self.assertRaises(conversation.ImageGenerationError) as caught:
                conversation._generate_single_image(
                    conversation.ConversationRequest(prompt="cat", model="gpt-image-2"),
                    1,
                    1,
                )

        self.assertEqual(str(caught.exception), "image generation timed out")
        self.assertEqual(caught.exception.conversation_id, "conv-timeout")


if __name__ == "__main__":
    unittest.main()
