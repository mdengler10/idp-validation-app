"""Send forms to MuleSoft IDP: either a custom Cloudhub URL or the official Document Action API."""
import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import httpx

from app.assistrx_extract import (
    idp_body_extraction_incomplete,
    idp_body_has_meaningful_fields,
)
from app.logging_setup import LOG_FILE
from app.request_context import get_request_id

logger = logging.getLogger(__name__)

# Max response body chars written to logs (secrets must not appear in bodies)
_LOG_BODY_MAX = 8000

DNS_ERRNO8_HINT = (
    " If this URL works in your browser, the app process may not have the same network access. "
    "Try running uvicorn from a normal Terminal (outside Cursor): "
    "cd /path/to/idp-validation-app && source .venv/bin/activate && uvicorn app.main:app --reload --host 127.0.0.1 --port 8000. "
    "If you use a corporate proxy, set HTTP_PROXY and HTTPS_PROXY in the environment before starting uvicorn."
)


def _is_dns_errno8(err: str) -> bool:
    return "nodename" in err or "servname" in err or "Errno 8" in err


def _request_error_message(url: str, e: Exception) -> str:
    err = str(e)
    msg = f"Request failed for URL {url}: {err}"
    if _is_dns_errno8(err):
        msg += DNS_ERRNO8_HINT
    return msg

# Production Connected App (assistrx-idp-enrollment-project); override via env if needed.
IDP_CLIENT_ID = os.environ.get("IDP_CLIENT_ID", "1fc1acdb959d44d98f021e0fdcdcc263").strip()
IDP_CLIENT_SECRET = os.environ.get("IDP_CLIENT_SECRET", "668a6D2441f94Ced877fBBA9ad8e39a1").strip()
ANYPOINT_DOMAIN = os.environ.get("ANYPOINT_DOMAIN", "anypoint.mulesoft.com").strip()

# Terminal statuses for execution – stop polling
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "MANUAL_VALIDATION_REQUIRED", "PARTIAL_SUCCESS"}
POLL_INTERVAL_SEC = 2
POLL_TIMEOUT_SEC = 300  # 5 min — IDP can stay IN_PROGRESS for a while
# After this many consecutive SUCCEEDED polls with classification-only body, stop waiting.
STUB_SUCCEEDED_GIVEUP = int(os.environ.get("IDP_STUB_POLL_MAX", "5"))
POLL_QUERY_PARAMS = {"valueOnly": "false"}  # full field payloads (matches AssistRx Mule poll)


def _execution_has_fields(body: dict[str, Any]) -> bool:
    """True if the execution JSON includes extracted field values (not just status/id)."""
    return idp_body_has_meaningful_fields(body)


def _execution_ready(body: dict[str, Any]) -> bool:
    """True when poll body has real extraction fields, not a classification-only stub."""
    return _execution_has_fields(body) and not idp_body_extraction_incomplete(body)


async def _fetch_execution(
    client: httpx.AsyncClient,
    url: str,
    token: str,
) -> httpx.Response:
    return await client.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=POLL_QUERY_PARAMS,
    )


def _domain_and_idp_host(platform: str) -> tuple[str, str]:
    """Return (anypoint_domain, idp_host_pattern with {region} placeholder)."""
    platform = (platform or "production").strip().lower()
    if platform == "staging":
        return ("stgx.anypoint.mulesoft.com", "idp-rt.{region}.stgx.anypoint.mulesoft.com")
    if platform == "eu":
        return ("eu1.anypoint.mulesoft.com", "idp-rt.{region}.eu1.anypoint.mulesoft.com")
    return ("anypoint.mulesoft.com", "idp-rt.{region}.anypoint.mulesoft.com")


def _log_client_id_hint(client_id: str) -> str:
    """Non-secret hint for logs (first chars only)."""
    if not client_id:
        return "(empty)"
    if len(client_id) <= 8:
        return f"len={len(client_id)}"
    return f"prefix={client_id[:8]}… len={len(client_id)}"


def _log_prefix() -> str:
    return f"request_id={get_request_id()}"


def _truncate_for_log(text: str, limit: int = _LOG_BODY_MAX) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated, total={len(text)} chars]"


def _response_body_for_log(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return _truncate_for_log(json.dumps(data, default=str))
    except Exception:
        return _truncate_for_log(resp.text or "")


def _log_http_error(
    phase: str,
    url: str,
    resp: httpx.Response,
    elapsed_ms: float,
    *,
    execution_id: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    msg = (
        f"IDP {phase} HTTP {resp.status_code} elapsed_ms={elapsed_ms:.1f} url={url} "
        f"headers={_interesting_response_headers(resp)}"
    )
    if execution_id:
        msg += f" execution_id={execution_id}"
    if extra:
        msg += f" extra={extra}"
    msg += f" body={_response_body_for_log(resp)}"
    logger.error("%s %s", _log_prefix(), msg)


def _idp_error_result(
    message: str,
    *,
    status_code: Optional[int] = None,
    body: Any = None,
    execution_id: Optional[str] = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "error": message,
        "request_id": get_request_id(),
        "log_file": str(LOG_FILE),
    }
    if status_code is not None:
        out["http_status"] = status_code
    if body is not None:
        out["body"] = body
    if execution_id:
        out["executionId"] = execution_id
    return out


def _interesting_response_headers(resp: httpx.Response) -> dict[str, str]:
    """Headers that often explain OAuth / gateway failures (no secrets)."""
    keys = (
        "content-type",
        "www-authenticate",
        "x-request-id",
        "x-amzn-requestid",
        "x-amzn-errortype",
        "cf-ray",
        "server",
    )
    out: dict[str, str] = {}
    for k in keys:
        v = resp.headers.get(k)
        if v:
            out[k] = v[:500]
    return out


async def get_anypoint_token(
    client_id: str, client_secret: str, anypoint_domain: str | None = None
) -> dict[str, Any]:
    """Get OAuth2 access token using client credentials (Connected App)."""
    client_id = (client_id or IDP_CLIENT_ID).strip()
    client_secret = (client_secret or IDP_CLIENT_SECRET).strip()
    if not client_id or not client_secret:
        return {"ok": False, "error": "Missing client_id or client_secret. Set in form or IDP_CLIENT_ID / IDP_CLIENT_SECRET."}
    domain = (anypoint_domain or ANYPOINT_DOMAIN or "anypoint.mulesoft.com").strip()
    url = f"https://{domain}/accounts/api/v2/oauth2/token"
    logger.info(
        "Anypoint OAuth token request starting url=%s client_id=%s secret_len=%s",
        url,
        _log_client_id_hint(client_id),
        len(client_secret),
    )
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/json"},
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if not resp.is_success:
            body = (resp.text or "")[:4000]
            logger.warning(
                "Anypoint OAuth token HTTP error status=%s elapsed_ms=%.1f url=%s headers=%s body=%s",
                resp.status_code,
                elapsed_ms,
                url,
                _interesting_response_headers(resp),
                body,
            )
            user_bit = (resp.text or "")[:800].strip()
            detail = f"Token request failed: {resp.status_code}"
            if user_bit:
                detail += f" — {user_bit}"
            detail += " (Full response is in server logs under 'Anypoint OAuth token HTTP error'.)"
            return {"ok": False, "error": detail}
        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            logger.warning("Anypoint OAuth token JSON missing access_token keys=%s body=%s", list(data.keys())[:20], str(data)[:500])
            return {"ok": False, "error": "No access_token in response."}
        logger.info("Anypoint OAuth token OK elapsed_ms=%.1f", elapsed_ms)
        return {"ok": True, "access_token": access_token}
    except httpx.RequestError as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.warning(
            "Anypoint OAuth token network error elapsed_ms=%.1f url=%s type=%s err=%r",
            elapsed_ms,
            url,
            type(e).__name__,
            e,
            exc_info=True,
        )
        return {"ok": False, "error": _request_error_message(url, e)}


def _idp_base_url(region: str, platform: str) -> str:
    _, host_pattern = _domain_and_idp_host(platform)
    r = (region or "").strip()
    return f"https://{host_pattern.format(region=r)}"


async def execute_idp_document_action(
    region: str,
    org_id: str,
    action_id: str,
    action_version: str,
    file_content: bytes,
    filename: str,
    content_type: str | None,
    client_id: str | None,
    client_secret: str | None,
    platform: str | None = None,
    callback_url: str | None = None,
) -> dict[str, Any]:
    """
    Execute a published IDP Document Action: get token, POST file to executions, poll until done, return result.
    platform: "production" | "staging" | "eu" — which Anypoint domain/IDP host to use.
    callback_url: optional; if set, IDP will call this URL when execution finishes (per connector doc).
    """
    region = (region or "").strip()
    org_id = (org_id or "").strip()
    action_id = (action_id or "").strip()
    action_version = (action_version or "").strip()
    if not all([region, org_id, action_id, action_version]):
        return _idp_error_result("Missing region, org_id, action_id, or action_version.")

    logger.info(
        "%s IDP execute start platform=%s region=%s org_id=%s action_id=%s version=%s "
        "file=%s bytes=%s content_type=%s client_id=%s",
        _log_prefix(),
        (platform or "production").strip().lower(),
        region,
        org_id,
        action_id,
        action_version,
        filename or "document",
        len(file_content) if file_content else 0,
        content_type or "application/octet-stream",
        _log_client_id_hint(client_id or ""),
    )

    anypoint_domain, _ = _domain_and_idp_host(platform)
    token_result = await get_anypoint_token(client_id or "", client_secret or "", anypoint_domain)
    if not token_result.get("ok"):
        token_result["request_id"] = get_request_id()
        token_result["log_file"] = str(LOG_FILE)
        return token_result
    token = token_result["access_token"]
    base = _idp_base_url(region, platform)
    executions_path = f"/api/v1/organizations/{org_id}/actions/{action_id}/versions/{action_version}/executions"

    # POST file to start execution (optional callback: IDP will call URL when done)
    post_url = f"{base}{executions_path}"
    files: dict[str, tuple[str, bytes | str, str]] = {
        "file": (filename or "document", file_content, content_type or "application/octet-stream")
    }
    callback_val = (callback_url or "").strip()
    if callback_val:
        files["callback"] = ("callback", json.dumps({"noAuthUrl": callback_val}), "text/plain")
    t_post = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.post(post_url, files=files, headers=headers)
    except httpx.RequestError as e:
        elapsed_ms = (time.perf_counter() - t_post) * 1000.0
        logger.warning(
            "%s IDP execute POST network error elapsed_ms=%.1f url=%s type=%s err=%r",
            _log_prefix(),
            elapsed_ms,
            post_url,
            type(e).__name__,
            e,
            exc_info=True,
        )
        return _idp_error_result(_request_error_message(post_url, e))

    elapsed_post_ms = (time.perf_counter() - t_post) * 1000.0
    if not resp.is_success:
        _log_http_error("execute POST", post_url, resp, elapsed_post_ms)
        try:
            err_body = resp.json()
        except Exception:
            err_body = {"_text": _truncate_for_log(resp.text or "", 2000), "_status_code": resp.status_code}
        user_msg = f"IDP execute failed: HTTP {resp.status_code}. See server log ({LOG_FILE}) with request_id={get_request_id()}."
        return _idp_error_result(user_msg, status_code=resp.status_code, body=err_body)

    try:
        start_body = resp.json()
    except Exception as parse_err:
        logger.error(
            "%s IDP execute POST response not JSON elapsed_ms=%.1f url=%s err=%s raw=%s",
            _log_prefix(),
            elapsed_post_ms,
            post_url,
            parse_err,
            _truncate_for_log(resp.text or "", 2000),
        )
        return _idp_error_result(
            "IDP execute response was not JSON.",
            body={"_raw": _truncate_for_log(resp.text or "", 2000)},
        )

    execution_id = start_body.get("id") or start_body.get("executionId")
    logger.info(
        "%s IDP execute POST ok elapsed_ms=%.1f execution_id=%s status=%s keys=%s",
        _log_prefix(),
        elapsed_post_ms,
        execution_id,
        start_body.get("status"),
        list(start_body.keys())[:25] if isinstance(start_body, dict) else [],
    )
    if not execution_id:
        logger.error(
            "%s IDP execute POST missing execution id body=%s",
            _log_prefix(),
            _truncate_for_log(json.dumps(start_body, default=str)),
        )
        return _idp_error_result("No execution id in IDP response.", body=start_body)

    status = start_body.get("status", "")
    if status in TERMINAL_STATUSES:
        return {"ok": status == "SUCCEEDED", "status": status, "body": start_body}

    # Poll for result. Connector/OpenAPI spec: GET .../executions/{executionId} (no /v2). Try v1 first, then v2 on 404.
    result_path_v1 = f"{executions_path}/{execution_id}"
    result_path_v2 = f"{executions_path}/{execution_id}/v2"
    poll_url = f"{base}{result_path_v1}"
    use_v2_fallback = False
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    last_status: str = ""
    last_keys: list[str] = []
    last_body_snippet: str = ""
    poll_get_retries = 3
    poll_tick = 0
    stub_succeeded_ticks = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        if use_v2_fallback:
            poll_url = f"{base}{result_path_v2}"
        r = None
        last_poll_error: Exception | None = None
        for attempt in range(poll_get_retries):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.get(
                        poll_url,
                        headers={"Authorization": f"Bearer {token}"},
                        params=POLL_QUERY_PARAMS,
                    )
                break
            except httpx.RequestError as e:
                last_poll_error = e
                err_str = str(e)
                logger.warning(
                    "%s Poll GET failed (attempt %s/%s): URL=%s type=%s error=%s",
                    _log_prefix(),
                    attempt + 1,
                    poll_get_retries,
                    poll_url,
                    type(e).__name__,
                    err_str or repr(e),
                )
                if attempt < poll_get_retries - 1:
                    await asyncio.sleep(2)
        if last_poll_error is not None:
            e = last_poll_error
            return _idp_error_result(
                _request_error_message(poll_url, e),
                execution_id=execution_id,
            )
        if not r.is_success:
            if r.status_code == 404 and not use_v2_fallback:
                use_v2_fallback = True
                logger.warning("%s Poll 404 with v1 path, retrying with /v2", _log_prefix())
                continue
            _log_http_error("poll GET", poll_url, r, 0, execution_id=execution_id)
            return _idp_error_result(
                f"IDP poll failed: HTTP {r.status_code}. See server log ({LOG_FILE}) request_id={get_request_id()}.",
                status_code=r.status_code,
                execution_id=execution_id,
            )
        try:
            result = r.json()
        except Exception:
            result = {"_raw": r.text[:1000]}
        # API may use "status" at top level or under execution; normalize to uppercase
        status = (
            result.get("status")
            or result.get("executionStatus")
            or result.get("state")
            or (isinstance(result.get("execution"), dict) and result["execution"].get("status"))
            or ""
        )
        if isinstance(status, str):
            status = status.strip().upper().replace(" ", "_")
        else:
            status = ""
        last_status = status
        last_keys = list(result.keys()) if isinstance(result, dict) else []
        last_body_snippet = r.text[:300] if r.text else ""
        poll_tick += 1
        if status in TERMINAL_STATUSES or poll_tick == 1 or poll_tick % 20 == 0:
            logger.info(
                "%s IDP poll tick=%s execution_id=%s status=%s keys=%s use_v2=%s",
                _log_prefix(),
                poll_tick,
                execution_id,
                status or "(empty)",
                last_keys,
                use_v2_fallback,
            )
        if status in TERMINAL_STATUSES:
            if status == "SUCCEEDED" and not _execution_ready(result):
                logger.warning(
                    "%s IDP SUCCEEDED but extraction not ready (keys=%s); refetching poll URL then /v2",
                    _log_prefix(),
                    last_keys,
                )
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        refetch = await _fetch_execution(client, poll_url, token)
                        if refetch.is_success:
                            refetch_body = refetch.json()
                            if _execution_ready(refetch_body):
                                result = refetch_body
                        if not _execution_ready(result):
                            v2_url = f"{base}{result_path_v2}"
                            v2 = await _fetch_execution(client, v2_url, token)
                            if v2.is_success:
                                v2_body = v2.json()
                                if _execution_ready(v2_body):
                                    result = v2_body
                                    logger.info("%s IDP fields loaded from GET .../v2", _log_prefix())
                            elif v2.status_code != 404:
                                _log_http_error("poll GET /v2", v2_url, v2, 0, execution_id=execution_id)
                except Exception as refetch_err:
                    logger.warning(
                        "%s IDP refetch for fields failed: %s",
                        _log_prefix(),
                        refetch_err,
                        exc_info=True,
                    )
                if not _execution_ready(result) and time.monotonic() < deadline - POLL_INTERVAL_SEC:
                    stub_succeeded_ticks += 1
                    if stub_succeeded_ticks >= STUB_SUCCEEDED_GIVEUP:
                        logger.warning(
                            "%s IDP SUCCEEDED but enrollment fields never appeared after %s polls; giving up",
                            _log_prefix(),
                            stub_succeeded_ticks,
                        )
                        return _idp_error_result(
                            "IDP reported SUCCEEDED but returned no enrollment field data "
                            "(classification-only response). The document may not be mapped in this "
                            "enrollment action version yet — check IDP classification/routing for this "
                            "form type (e.g. Ipsen/Somatuline).",
                            execution_id=execution_id,
                            body=result,
                        )
                    logger.warning(
                        "%s IDP SUCCEEDED with classification-only/stub body; continuing poll (%s/%s)",
                        _log_prefix(),
                        stub_succeeded_ticks,
                        STUB_SUCCEEDED_GIVEUP,
                    )
                    continue
                stub_succeeded_ticks = 0
            logger.info(
                "%s IDP finished execution_id=%s status=%s ready=%s",
                _log_prefix(),
                execution_id,
                status,
                _execution_ready(result),
            )
            out = {"ok": status == "SUCCEEDED", "status": status, "body": result}
            out["request_id"] = get_request_id()
            return out
        if status and status not in ("IN_PROGRESS", "ACKNOWLEDGED", "RESULTS_PENDING", "PENDING"):
            logger.warning(
                "%s IDP returned unknown status %r, treating as terminal",
                _log_prefix(),
                status,
            )
            return _idp_error_result(
                f"IDP returned unexpected status: {status}",
                execution_id=execution_id,
                body=result,
            )
    err_msg = "Timed out waiting for execution result."
    if last_status == "IN_PROGRESS":
        err_msg += " Execution was still in progress. IDP may be slow or queued; try again in a few minutes or set a Callback URL to be notified when done."
    err_msg += f" Poll URL: {poll_url}."
    if last_status or last_keys:
        err_msg += f" Last poll status: {last_status or '(empty)'}, response keys: {last_keys}."
    if last_body_snippet:
        err_msg += f" Last response snippet: {last_body_snippet!r}"
    err_msg += f" Log: {LOG_FILE} request_id={get_request_id()}."
    logger.error("%s IDP poll timeout execution_id=%s %s", _log_prefix(), execution_id, err_msg)
    return _idp_error_result(err_msg, execution_id=execution_id)
