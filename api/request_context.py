from __future__ import annotations

from contextvars import ContextVar, Token

from fastapi import Request


_current_request: ContextVar[Request | None] = ContextVar("current_request", default=None)


def get_current_request() -> Request | None:
    return _current_request.get()


def set_current_request(request: Request) -> Token[Request | None]:
    return _current_request.set(request)


def reset_current_request(token: Token[Request | None]) -> None:
    _current_request.reset(token)
