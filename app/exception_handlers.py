"""Global exception handlers — log full trace, return request_id to client."""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_setup import LOG_FILE
from app.request_context import get_request_id

logger = logging.getLogger(__name__)


def _error_payload(
    message: str,
    *,
    status_code: int = 500,
    detail: str | None = None,
    exc_type: str | None = None,
) -> dict:
    body: dict = {
        "ok": False,
        "error": message,
        "request_id": get_request_id(),
        "log_file": str(LOG_FILE),
    }
    if detail:
        body["detail"] = detail
    if exc_type:
        body["exception_type"] = exc_type
    body["status_code"] = status_code
    return body


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled exception path=%s method=%s",
        request.url.path,
        request.method,
    )
    return JSONResponse(
        status_code=500,
        content=_error_payload(
            f"Server error: {type(exc).__name__}",
            detail=str(exc)[:2000],
            exc_type=type(exc).__name__,
        ),
        headers={"X-Request-Id": get_request_id()},
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code >= 500:
        logger.error(
            "HTTPException %s path=%s detail=%s",
            exc.status_code,
            request.url.path,
            exc.detail,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(
            str(exc.detail),
            status_code=exc.status_code,
        ),
        headers={"X-Request-Id": get_request_id()},
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    logger.warning(
        "Validation error path=%s errors=%s",
        request.url.path,
        exc.errors(),
    )
    return JSONResponse(
        status_code=422,
        content={
            **_error_payload("Validation error", status_code=422),
            "errors": exc.errors(),
        },
        headers={"X-Request-Id": get_request_id()},
    )
