from __future__ import annotations

import hashlib
import hmac

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.auth_service import auth_service
from services.config import config
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from services.protocol.error_response import anthropic_error_response, error_message_from_detail, openai_error_response


def _is_openai_compatible_path(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/")


def _is_anthropic_messages_path(path: str) -> bool:
    return path == "/v1/messages"


def _header_count(request: Request, name: str) -> int:
    target = name.lower().encode("latin-1")
    return sum(1 for key, _ in request.scope.get("headers") or [] if key.lower() == target)


def _token_fingerprint(token: str) -> str:
    value = str(token or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _extract_bearer_token(authorization: str | None) -> tuple[str, str]:
    scheme, _, value = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return scheme, ""
    return scheme, value.strip()


def _safe_header(request: Request, name: str) -> str:
    return str(request.headers.get(name) or "").strip()


def _auth_state_for(token: str) -> dict[str, object] | None:
    if not str(token or "").strip():
        return None
    try:
        return auth_service.debug_auth_state(token)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _log_unauthorized_request(request: Request, detail: object) -> None:
    authorization = _safe_header(request, "authorization")
    scheme, bearer_token = _extract_bearer_token(authorization)
    x_api_key = _safe_header(request, "x-api-key")
    admin_key = str(config.auth_key or "").strip()
    query_keys = sorted(set(request.query_params.keys()))
    diagnostic: dict[str, object] = {
        "status_code": 401,
        "error": error_message_from_detail(detail) or "unauthorized",
        "method": request.method,
        "path": request.url.path,
        "query_keys": query_keys[:20],
        "host": _safe_header(request, "host"),
        "client_host": request.client.host if request.client else "",
        "user_agent": _safe_header(request, "user-agent"),
        "content_type": _safe_header(request, "content-type"),
        "authorization_present": bool(authorization),
        "authorization_header_count": _header_count(request, "authorization"),
        "authorization_header_len": len(authorization),
        "authorization_scheme": scheme,
        "authorization_bearer_len": len(bearer_token),
        "authorization_bearer_fingerprint": _token_fingerprint(bearer_token),
        "x_api_key_present": bool(x_api_key),
        "x_api_key_supported_on_path": _is_anthropic_messages_path(request.url.path),
        "x_api_key_header_count": _header_count(request, "x-api-key"),
        "x_api_key_len": len(x_api_key),
        "x_api_key_fingerprint": _token_fingerprint(x_api_key),
        "legacy_admin_configured": bool(admin_key),
        "legacy_admin_match": bool(
            bearer_token
            and admin_key
            and hmac.compare_digest(bearer_token, admin_key)
        ),
        "x_forwarded_for": _safe_header(request, "x-forwarded-for"),
        "x_real_ip": _safe_header(request, "x-real-ip"),
        "cf_connecting_ip": _safe_header(request, "cf-connecting-ip"),
        "cf_ray": _safe_header(request, "cf-ray"),
        "x_request_id": _safe_header(request, "x-request-id"),
        "x_forwarded_host": _safe_header(request, "x-forwarded-host"),
        "x_forwarded_proto": _safe_header(request, "x-forwarded-proto"),
    }
    selected_token = bearer_token or x_api_key
    auth_state = _auth_state_for(selected_token)
    if auth_state is not None:
        diagnostic["diagnostic_token_source"] = "authorization_bearer" if bearer_token else "x-api-key"
        diagnostic["diagnostic_token_auth_state"] = auth_state
    if bearer_token and x_api_key and x_api_key != bearer_token:
        x_api_key_state = _auth_state_for(x_api_key)
        if x_api_key_state is not None:
            diagnostic["x_api_key_auth_state"] = x_api_key_state

    try:
        log_service.add(LOG_TYPE_ACCOUNT, "鉴权失败 401", diagnostic)
    except Exception:
        pass

    print(
        "[auth] 401 "
        f"path={request.url.path} "
        f"method={request.method} "
        f"client={diagnostic['client_host']} "
        f"auth_present={diagnostic['authorization_present']} "
        f"scheme={scheme or '-'} "
        f"bearer_len={len(bearer_token)} "
        f"bearer_fp={diagnostic['authorization_bearer_fingerprint'] or '-'} "
        f"x_api_key_present={diagnostic['x_api_key_present']} "
        f"x_request_id={diagnostic['x_request_id'] or '-'}",
        flush=True,
    )


def _compatible_error_response(
    request: Request,
    detail: object,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    if _is_anthropic_messages_path(request.url.path):
        return anthropic_error_response(detail, status_code, headers=headers)
    return openai_error_response(detail, status_code, headers=headers)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 401:
            _log_unauthorized_request(request, exc.detail)
        if _is_openai_compatible_path(request.url.path):
            return _compatible_error_response(request, exc.detail, exc.status_code, exc.headers)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": jsonable_encoder(exc.detail)},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        if _is_openai_compatible_path(request.url.path):
            return _compatible_error_response(request, exc.errors(), 422)
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
