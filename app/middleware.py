"""HTTP middleware for request correlation and access logging."""
from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.request_context import get_request_id, new_request_id, set_request_id

logger = logging.getLogger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("x-request-id") or new_request_id()
        set_request_id(rid)
        request.state.request_id = rid
        t0 = time.perf_counter()
        path = request.url.path
        is_idp = "/idp/" in path or "/run-test" in path or "idp-credentials" in path
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            logger.exception(
                "Request failed path=%s method=%s elapsed_ms=%.1f",
                path,
                request.method,
                elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if is_idp or response.status_code >= 400:
            logger.info(
                "Request done path=%s method=%s status=%s elapsed_ms=%.1f",
                path,
                request.method,
                response.status_code,
                elapsed_ms,
            )
        response.headers["X-Request-Id"] = get_request_id()
        return response
