from __future__ import annotations

import os
from unittest import TestCase, mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.errors import install_exception_handlers
from services.log_service import LOG_TYPE_ACCOUNT


class AuthFailureLoggingTests(TestCase):
    def test_unauthorized_request_logs_redacted_diagnostics(self) -> None:
        app = FastAPI()
        install_exception_handlers(app)

        @app.get("/v1/probe")
        async def probe():
            raise HTTPException(status_code=401, detail={"error": "密钥无效或已失效，请重新登录"})

        auth_state = {
            "memory": {"candidate_match": "none"},
            "storage": {"candidate_match": "enabled", "candidate_match_id": "key-1"},
        }
        token = "sk-secret-value"
        with (
            mock.patch("api.errors.auth_service.debug_auth_state", return_value=auth_state),
            mock.patch("api.errors.log_service.add") as add_log,
            mock.patch("builtins.print"),
        ):
            response = TestClient(app).get(
                "/v1/probe?secret=hidden&foo=bar",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Request-Id": "req-1",
                    "X-Forwarded-For": "203.0.113.10",
                    "User-Agent": "unit-test",
                },
            )

        self.assertEqual(response.status_code, 401)
        add_log.assert_called_once()
        log_type, summary, detail = add_log.call_args.args
        self.assertEqual(log_type, LOG_TYPE_ACCOUNT)
        self.assertEqual(summary, "鉴权失败 401")
        self.assertEqual(detail["path"], "/v1/probe")
        self.assertEqual(detail["query_keys"], ["foo", "secret"])
        self.assertEqual(detail["authorization_present"], True)
        self.assertEqual(detail["authorization_scheme"], "Bearer")
        self.assertEqual(detail["authorization_bearer_len"], len(token))
        self.assertEqual(detail["diagnostic_token_source"], "authorization_bearer")
        self.assertEqual(detail["diagnostic_token_auth_state"], auth_state)
        self.assertNotIn(token, str(detail))
