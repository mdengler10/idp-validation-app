"""Per-request correlation ID for logs and API error responses."""
from __future__ import annotations

import uuid
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def set_request_id(value: str) -> None:
    _request_id.set(value or new_request_id())


def get_request_id() -> str:
    rid = _request_id.get()
    return rid or new_request_id()
