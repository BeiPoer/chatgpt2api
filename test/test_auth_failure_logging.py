from __future__ import annotations

import os
from unittest import TestCase, mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.errors import install_exception_handlers
from api.support import require_identity
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
            mock.patch("api.auth_diagnostics.auth_service.debug_auth_state", return_value=auth_state),
            mock.patch("api.auth_diagnostics.log_service.add") as add_log,
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
        self.assertEqual(detail["log_source"], "exception_handler")
        self.assertEqual(detail["query_keys"], ["foo", "secret"])
        self.assertEqual(detail["authorization_present"], True)
        self.assertEqual(detail["authorization_scheme"], "Bearer")
        self.assertEqual(detail["authorization_bearer_len"], len(token))
        self.assertEqual(detail["diagnostic_token_source"], "authorization_bearer")
        self.assertEqual(detail["diagnostic_token_auth_state"], auth_state)
        self.assertNotIn(token, str(detail))

    def test_direct_unauthorized_response_is_logged(self) -> None:
        app = FastAPI()
        install_exception_handlers(app)

        @app.get("/plain-401")
        async def plain_401():
            return JSONResponse(status_code=401, content={"detail": "nope"})

        with (
            mock.patch("api.auth_diagnostics.auth_service.debug_auth_state", return_value={"memory": {}, "storage": {}}),
            mock.patch("api.auth_diagnostics.log_service.add") as add_log,
            mock.patch("builtins.print"),
        ):
            response = TestClient(app).get("/plain-401", headers={"Authorization": "Bearer wrong"})

        self.assertEqual(response.status_code, 401)
        add_log.assert_called_once()
        _, summary, detail = add_log.call_args.args
        self.assertEqual(summary, "鉴权失败 401")
        self.assertEqual(detail["path"], "/plain-401")
        self.assertEqual(detail["log_source"], "response_middleware")

    def test_require_identity_logs_before_raising(self) -> None:
        app = FastAPI()
        install_exception_handlers(app)

        @app.get("/v1/guarded")
        async def guarded(authorization: str | None = Header(default=None)):
            require_identity(authorization)
            return {"ok": True}

        auth_state = {
            "memory": {"candidate_match": "none"},
            "storage": {"candidate_match": "none"},
        }
        with (
            mock.patch("api.auth_diagnostics.auth_service.debug_auth_state", return_value=auth_state),
            mock.patch("api.auth_diagnostics.log_service.add") as add_log,
            mock.patch("builtins.print"),
        ):
            response = TestClient(app).get("/v1/guarded", headers={"Authorization": "Bearer wrong"})

        self.assertEqual(response.status_code, 401)
        add_log.assert_called_once()
        _, summary, detail = add_log.call_args.args
        self.assertEqual(summary, "鉴权失败 401")
        self.assertEqual(detail["path"], "/v1/guarded")
        self.assertEqual(detail["log_source"], "require_identity")
        self.assertEqual(detail["diagnostic_token_auth_state"], auth_state)
