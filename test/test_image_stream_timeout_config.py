from __future__ import annotations

import unittest
from unittest import mock

import services.openai_backend_api as backend_module
from services.openai_backend_api import ChatRequirements, ImageStreamHardTimeoutError, OpenAIBackendAPI


class FakeResponse:
    status_code = 200
    text = ""
    headers = {}

    def json(self):
        return {}


class FakeSession:
    def __init__(self) -> None:
        self.headers = {}
        self.post_timeout = None

    def post(self, *args, **kwargs):
        self.post_timeout = kwargs.get("timeout")
        return FakeResponse()


class FakeRaw:
    status = 200
    headers = {"content-type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return b"{}"


class FakeClosedStreamResponse:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def iter_lines(self):
        if self.closed:
            raise RuntimeError("stream closed")
        yield b"data: {}\n"


class ImmediateTimer:
    def __init__(self, _interval, function) -> None:
        self.function = function
        self.daemon = False

    def start(self) -> None:
        self.function()

    def cancel(self) -> None:
        pass


class ImageStreamTimeoutConfigTests(unittest.TestCase):
    def _backend(self) -> OpenAIBackendAPI:
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.base_url = "https://chatgpt.com"
        backend.access_token = "token"
        backend.session = FakeSession()
        return backend

    def test_image_sse_timeout_uses_image_timeout_config(self) -> None:
        backend = self._backend()
        with mock.patch.dict(backend_module.config.data, {"image_poll_timeout_secs": 200}):
            backend._start_image_generation("cat", ChatRequirements(token="req"), "conduit", "gpt-image-2")

        self.assertEqual(backend.session.post_timeout, 200)

    def test_codex_image_timeout_uses_image_timeout_config(self) -> None:
        backend = self._backend()
        seen: dict[str, object] = {}

        def fake_urlopen(request, timeout):
            seen["timeout"] = timeout
            return FakeRaw()

        with (
            mock.patch.dict(backend_module.config.data, {"image_poll_timeout_secs": 200}),
            mock.patch.object(backend_module.account_service, "get_account", return_value={"source_type": "codex"}),
            mock.patch.object(backend_module.account_service, "_decode_jwt_payload", return_value={}),
            mock.patch.object(backend_module.urllib.request, "urlopen", fake_urlopen),
        ):
            list(backend.iter_codex_image_response_events("cat"))

        self.assertEqual(seen["timeout"], 200)

    def test_capped_sse_reader_raises_timeout_after_forced_close(self) -> None:
        backend = self._backend()
        response = FakeClosedStreamResponse()

        with mock.patch.object(backend_module.threading, "Timer", ImmediateTimer):
            with self.assertRaises(ImageStreamHardTimeoutError) as caught:
                list(backend._iter_sse_payloads_capped(response, 200))

        self.assertTrue(response.closed)
        self.assertIn("超时", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
