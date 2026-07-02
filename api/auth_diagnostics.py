from __future__ import annotations

import hashlib
import hmac

from fastapi import Request

from api.request_context import get_current_request
from services.auth_service import auth_service
from services.config import config
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from services.protocol.error_response import error_message_from_detail


def is_anthropic_messages_path(path: str) -> bool:
    return path == "/v1/messages"


def _header_count(request: Request, name: str) -> int:
    target = name.lower().encode("latin-1")
    return sum(1 for key, _ in request.scope.get("headers") or [] if key.lower() == target)


def _token_fingerprint(token: str) -> str:
    value = str(token or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def extract_bearer_token_parts(authorization: str | None) -> tuple[str, str]:
    scheme, _, value = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return scheme, ""
    return scheme, value.strip()


def _safe_header(request: Request | None, name: str) -> str:
    if request is None:
        return ""
    return str(request.headers.get(name) or "").strip()


def _auth_state_for(token: str) -> dict[str, object] | None:
    if not str(token or "").strip():
        return None
    try:
        return auth_service.debug_auth_state(token)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def log_unauthorized_request(
    detail: object,
    *,
    source: str,
    authorization: str | None = None,
    request: Request | None = None,
) -> None:
    request = request or get_current_request()
    if request is not None:
        if bool(getattr(request.state, "auth_failure_logged", False)):
            return
        request.state.auth_failure_logged = True

    raw_authorization = _safe_header(request, "authorization")
    if authorization is not None:
        raw_authorization = str(authorization or "").strip()
    scheme, bearer_token = extract_bearer_token_parts(raw_authorization)
    x_api_key = _safe_header(request, "x-api-key")
    admin_key = str(config.auth_key or "").strip()
    path = request.url.path if request is not None else ""
    query_keys = sorted(set(request.query_params.keys())) if request is not None else []
    diagnostic: dict[str, object] = {
        "status_code": 401,
        "log_source": source,
        "error": error_message_from_detail(detail) or "unauthorized",
        "method": request.method if request is not None else "",
        "path": path,
        "query_keys": query_keys[:20],
        "host": _safe_header(request, "host"),
        "client_host": request.client.host if request is not None and request.client else "",
        "user_agent": _safe_header(request, "user-agent"),
        "content_type": _safe_header(request, "content-type"),
        "authorization_present": bool(raw_authorization),
        "authorization_header_count": _header_count(request, "authorization") if request is not None else 0,
        "authorization_header_len": len(raw_authorization),
        "authorization_scheme": scheme,
        "authorization_bearer_len": len(bearer_token),
        "authorization_bearer_fingerprint": _token_fingerprint(bearer_token),
        "x_api_key_present": bool(x_api_key),
        "x_api_key_supported_on_path": is_anthropic_messages_path(path),
        "x_api_key_header_count": _header_count(request, "x-api-key") if request is not None else 0,
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
    except Exception as exc:
        print(f"[auth] 401 log write failed: {type(exc).__name__}: {exc}", flush=True)

    print(
        "[auth] 401 "
        f"source={source} "
        f"path={path or '-'} "
        f"method={diagnostic['method'] or '-'} "
        f"client={diagnostic['client_host'] or '-'} "
        f"auth_present={diagnostic['authorization_present']} "
        f"scheme={scheme or '-'} "
        f"bearer_len={len(bearer_token)} "
        f"bearer_fp={diagnostic['authorization_bearer_fingerprint'] or '-'} "
        f"x_api_key_present={diagnostic['x_api_key_present']} "
        f"x_request_id={diagnostic['x_request_id'] or '-'}",
        flush=True,
    )
