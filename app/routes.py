"""API and web routes."""
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy.orm import Session

from app.db import get_db, init_db
from app.models import ExpectedDocument, Result, Run, SavedGroundTruth
from app.schemas import ExpectedBulkIn, ExpectedDocumentIn, IDPResultIn, OverrideIn, RunFromJsonIn, RunPatchIn
from app.idp_client import execute_idp_document_action
from app.compare import normalize_body_fields, PASS_ACCURACY_THRESHOLD_PCT
from app.service import (
    clear_override,
    create_run,
    create_run_from_json,
    delete_run,
    update_run_label,
    get_all_time_stats,
    get_expected,
    get_result_with_effective,
    get_run_field_stats,
    get_run_success_rate,
    has_undo_available,
    load_expected,
    load_expected_bulk,
    reset_metrics,
    restore_from_backup,
    set_override,
    submit_result,
    list_saved_ground_truth,
    create_saved_ground_truth,
    get_saved_ground_truth,
    delete_saved_ground_truth,
    set_idp_credentials,
    merge_idp_credentials_save,
    normalize_stored_credentials,
    get_idp_credentials,
    resolve_idp_execution_params,
    get_suite_overview,
    get_document_aggregate_stats,
    get_suite_summary,
    get_saved_ground_truth_by_document_id,
    get_latest_result_for_document,
    save_profile_ground_truth,
    run_profile_comparison,
)
from app.assistrx_extract import (
    extract_from_idp_body,
    gt_fields_for_extract_ui,
    has_meaningful_field_values,
    has_profile_field_values,
    idp_body_extraction_incomplete,
    profile_for_document_id,
)
from app.assistrx_profiles import get_profile, list_profiles
from app.logging_setup import LOG_FILE
from app.request_context import get_request_id

logger = logging.getLogger(__name__)

api_router = APIRouter()
web_router = APIRouter()
templates_dir = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
from app.display import field_display_name
templates.env.filters["field_display_name"] = field_display_name


def _dt_iso(dt: datetime | None) -> str | None:
    """Serialize timestamps; NULL rows must not crash on .isoformat()."""
    return dt.isoformat() if dt is not None else None


def _persist_idp_form_credentials(
    db: Session,
    platform: str | None,
    region: str | None,
    org_id: str | None,
    action_id: str | None,
    action_version: str | None,
    client_id: str | None,
    client_secret: str | None,
) -> None:
    """Save IDP form values when client credentials are present (suite/extract flows)."""
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if not cid or not secret:
        return
    merge_idp_credentials_save(
        db,
        {
            "platform": (platform or "production").strip().lower() or "production",
            "region": (region or "").strip(),
            "org_id": (org_id or "").strip(),
            "action_id": (action_id or "").strip(),
            "action_version": (action_version or "").strip(),
            "client_id": cid,
            "client_secret": secret,
        },
    )


# ---- Expected data ----
@api_router.post("/expected")
def api_load_expected(payload: ExpectedDocumentIn, db: Session = Depends(get_db)):
    load_expected(db, payload.document_id, payload.fields, payload.field_types)
    return {"ok": True, "document_id": payload.document_id}


@api_router.post("/expected/bulk")
def api_load_expected_bulk(payload: ExpectedBulkIn, db: Session = Depends(get_db)):
    n = load_expected_bulk(db, payload.documents, payload.field_types_global)
    return {"ok": True, "count": n}


@api_router.post("/expected/upload")
async def api_upload_expected(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload a single JSON file: object keyed by document_id, values are field objects."""
    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON must be an object keyed by document_id")
    n = load_expected_bulk(db, data)
    return {"ok": True, "count": n}


# ---- Runs and results ----
@api_router.post("/runs")
def api_create_run(label: str | None = None, db: Session = Depends(get_db)):
    r = create_run(db, label)
    return {"id": r.id, "label": r.label, "created_at": _dt_iso(r.created_at)}


@api_router.get("/runs")
def api_list_runs(db: Session = Depends(get_db)):
    runs = db.query(Run).order_by(Run.created_at.desc()).all()
    out = []
    for r in runs:
        rate = get_run_success_rate(db, r.id)
        out.append({
            "id": r.id,
            "label": r.label,
            "created_at": _dt_iso(r.created_at),
            "success_rate": rate,
        })
    return out


@api_router.post("/runs/from-json")
def api_create_run_from_json(payload: RunFromJsonIn, db: Session = Depends(get_db)):
    """Create run from two JSON objects (expected + IDP), both keyed by document_id."""
    run, count, warnings = create_run_from_json(
        db, payload.expected, payload.idp, payload.label, payload.idp_bodies
    )
    if run is None:
        raise HTTPException(
            400,
            warnings[0] if warnings else "Could not create run — check document_id keys match between expected and IDP.",
        )
    return {
        "id": run.id,
        "label": run.label,
        "created_at": _dt_iso(run.created_at),
        "submitted": count,
        "warnings": warnings,
    }


@api_router.post("/runs/{run_id}/results")
def api_submit_result(run_id: int, payload: IDPResultIn, db: Session = Depends(get_db)):
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    result = submit_result(db, run_id, payload.document_id, payload.fields)
    if result is None:
        raise HTTPException(404, f"No expected data for document_id={payload.document_id}")
    return {
        "id": result.id,
        "run_id": result.run_id,
        "document_id": result.document_id,
        "pass": result.pass_,
        "score": result.score,
        "total_fields": result.total_fields,
    }


@api_router.post("/runs/{run_id}/results/upload")
async def api_upload_idp_results(run_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Bulk: JSON object keyed by document_id, values are field objects."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON must be an object keyed by document_id")
    results = []
    for doc_id, fields in data.items():
        r = submit_result(db, run_id, doc_id, fields)
        if r:
            results.append({"document_id": doc_id, "pass": r.pass_, "score": r.score})
    return {"ok": True, "run_id": run_id, "submitted": len(results)}


@api_router.get("/runs/{run_id}/results")
def api_list_results(run_id: int, db: Session = Depends(get_db)):
    results = db.query(Result).filter(Result.run_id == run_id).all()
    out = []
    for r in results:
        eff = get_result_with_effective(db, r.id)
        if eff:
            out.append(eff)
    return out


@api_router.get("/runs/{run_id}/results/{document_id}")
def api_get_result_by_doc(run_id: int, document_id: str, db: Session = Depends(get_db)):
    result = db.query(Result).filter(Result.run_id == run_id, Result.document_id == document_id).first()
    if not result:
        raise HTTPException(404, "Result not found")
    eff = get_result_with_effective(db, result.id)
    if not eff:
        raise HTTPException(404, "Result not found")
    return eff


@api_router.post("/results/{result_id}/override")
def api_set_override(result_id: int, payload: OverrideIn, db: Session = Depends(get_db)):
    set_override(db, result_id, payload.field_name, payload.override_pass)
    eff = get_result_with_effective(db, result_id)
    return eff or {"ok": True}


@api_router.delete("/results/{result_id}/override/{field_name}")
def api_clear_override(result_id: int, field_name: str, db: Session = Depends(get_db)):
    clear_override(db, result_id, field_name)
    eff = get_result_with_effective(db, result_id)
    return eff or {"ok": True}


@api_router.get("/stats")
def api_get_stats(db: Session = Depends(get_db)):
    """All-time running tally: total fields, right, wrong, % correct across all runs."""
    return get_all_time_stats(db)


@api_router.get("/undo-available")
def api_undo_available(db: Session = Depends(get_db)):
    """True if the last action was a Reset and Undo can restore."""
    return {"available": has_undo_available(db)}


@api_router.post("/undo")
def api_undo_reset(db: Session = Depends(get_db)):
    """Restore runs/results/overrides from the backup made at last Reset. One-time; clears backup after."""
    if not has_undo_available(db):
        raise HTTPException(400, "Nothing to undo")
    restore_from_backup(db)
    return {"ok": True}


@api_router.post("/reset")
def api_reset_metrics(db: Session = Depends(get_db)):
    """Save backup (for Undo), then delete all runs (and results/overrides). Does not delete saved ground truth or expected_documents."""
    undo_available = reset_metrics(db)
    return {"ok": True, "undo_available": undo_available}


def _attach_request_meta(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure API responses include correlation id + log path for support."""
    out = dict(payload)
    out.setdefault("request_id", get_request_id())
    out.setdefault("log_file", str(LOG_FILE))
    return out


@api_router.get("/debug/log-info")
def api_log_info():
    """Where to find server logs when IDP or compare fails."""
    return {
        "log_file": str(LOG_FILE),
        "hint": "Search this file for your request_id from the error message.",
        "env": "Set IDP_LOG_LEVEL=DEBUG for verbose IDP HTTP traces.",
    }


@api_router.get("/idp/execution-target")
def api_idp_execution_target(
    document_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Resolved IDP endpoint for a document (profile overrides saved staging creds)."""
    _resolved, meta = resolve_idp_execution_params(db, document_id=document_id)
    return meta


@api_router.post("/idp/extract")
async def api_idp_extract(
    file: UploadFile = File(...),
    document_id: str | None = Form(None),
    platform: str = Form("production"),
    region: str | None = Form(None),
    org_id: str | None = Form(None),
    action_id: str | None = Form(None),
    action_version: str | None = Form(None),
    client_id: str | None = Form(None),
    client_secret: str | None = Form(None),
    callback_url: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Upload a form to a MuleSoft IDP Document Action (execute + poll for result)."""
    resolved, target_meta = resolve_idp_execution_params(
        db,
        document_id=document_id,
        platform=platform,
        region=region,
        org_id=org_id,
        action_id=action_id,
        action_version=action_version,
        client_id=client_id,
        client_secret=client_secret,
    )
    for w in target_meta.get("warnings") or []:
        logger.warning("api_idp_extract %s", w)
    logger.info(
        "api_idp_extract start filename=%s source=%s document_id=%s platform=%s region=%s org_id=%s action_id=%s version=%s",
        file.filename,
        target_meta.get("source"),
        document_id,
        resolved["platform"],
        resolved["region"],
        resolved["org_id"],
        resolved["action_id"],
        resolved["action_version"],
    )
    try:
        # Do not persist OAuth on extract — a failed or mis-matched run must not overwrite by_platform.production.
        content = await file.read()
        platform_value = resolved["platform"]
        result = await execute_idp_document_action(
            resolved["region"],
            resolved["org_id"],
            resolved["action_id"],
            resolved["action_version"],
            content,
            file.filename or "document",
            file.content_type,
            resolved["client_id"],
            resolved["client_secret"],
            platform_value,
            callback_url or None,
        )
        # If old server code returned a "URL" error, replace with clear message
        if not result.get("ok") and result.get("error") and (
            "IDP_CLOUDHUB" in result.get("error", "") or "No URL configured" in result.get("error", "")
        ):
            return _attach_request_meta({
                "ok": False,
                "error": (
                    "This app no longer uses a URL field. Restart the app server (stop and start uvicorn), "
                    "then hard-refresh this page (Ctrl+Shift+R). Use the Platform dropdown (Production / Staging / EU) instead."
                ),
            })
        # Normalize section-shaped response so UI gets one field per leaf (section_1.field_name, etc.)
        body = result.get("body") or {}
        extracted_fields: dict[str, Any] = {}
        profile = profile_for_document_id((document_id or "").strip()) if document_id else None
        if isinstance(body, dict):
            if profile:
                extracted_fields = gt_fields_for_extract_ui(body, profile)
                flat = extract_from_idp_body(profile, body)
                logger.info(
                    "api_idp_extract parsed_fields profile=%s count=%s ui_fields=%s",
                    profile.document_id,
                    len(flat),
                    len(extracted_fields),
                )
            result = {
                **result,
                "body": body,
                "body_normalized": normalize_body_fields(dict(body)),
                "extracted_fields": extracted_fields,
            }
        elif not isinstance(body, dict):
            result = {**result, "body": body}
        if not result.get("ok"):
            err = result.get("error") or ""
            if "401" in err and resolved.get("platform") == "production":
                err = (
                    f"{err} Production IDP requires a production Connected App (Client ID/Secret). "
                    "Open Extract & Edit → Platform = Production (US) → paste production credentials → "
                    "Save credentials. Staging Connected Apps cannot authenticate to anypoint.mulesoft.com for production orgs."
                )
                result = {**result, "error": err}
            logger.warning(
                "api_idp_extract IDP failed error=%s http_status=%s executionId=%s",
                result.get("error"),
                result.get("http_status"),
                result.get("executionId"),
            )
        else:
            logger.info(
                "api_idp_extract ok status=%s executionId=%s body_keys=%s",
                result.get("status"),
                result.get("executionId") or (body.get("id") if isinstance(body, dict) else None),
                list(body.keys())[:20] if isinstance(body, dict) else [],
            )
        return _attach_request_meta({
            **result,
            "idp_target": target_meta.get("target"),
            "idp_target_source": target_meta.get("source"),
            "idp_target_warnings": target_meta.get("warnings"),
        })
    except Exception as exc:
        logger.exception("api_idp_extract unhandled error")
        return _attach_request_meta({
            "ok": False,
            "error": f"Server error during IDP extract: {type(exc).__name__}: {exc}",
            "exception_type": type(exc).__name__,
        })


@api_router.patch("/runs/{run_id}")
def api_patch_run(run_id: int, payload: RunPatchIn, db: Session = Depends(get_db)):
    """Rename a run (label) from the Runs list."""
    run = update_run_label(db, run_id, payload.label)
    if not run:
        raise HTTPException(404, "Run not found")
    return {"id": run.id, "label": run.label}


@api_router.delete("/runs/{run_id}")
def api_delete_run(run_id: int, db: Session = Depends(get_db)):
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    delete_run(db, run_id)
    return {"ok": True}


# ---- Web UI ----
@web_router.get("/runs", response_class=HTMLResponse)
def page_runs(request: Request, db: Session = Depends(get_db)):
    runs = db.query(Run).order_by(Run.created_at.desc()).all()
    runs_with_rate = []
    for r in runs:
        stats = get_run_field_stats(db, r.id)
        # Show field-level % correct (not document-level 100%/0%)
        field_pct = stats["pct"] if stats["total_fields"] > 0 else None
        runs_with_rate.append({
            "run": r,
            "success_rate": field_pct,
            "total_fields": stats["total_fields"],
            "run_pass": stats.get("run_pass") if field_pct is not None else None,
            "pass_threshold_pct": PASS_ACCURACY_THRESHOLD_PCT,
        })
    all_time = get_all_time_stats(db)
    undo_available = has_undo_available(db)
    return templates.TemplateResponse(
        "runs.html",
        {
            "request": request,
            "runs_with_rate": runs_with_rate,
            "all_time": all_time,
            "undo_available": undo_available,
            "pass_threshold_pct": PASS_ACCURACY_THRESHOLD_PCT,
        },
    )


@web_router.get("/runs/new", response_class=HTMLResponse)
def page_new_run(request: Request):
    return templates.TemplateResponse("run_new.html", {"request": request})


@web_router.get("/runs/{run_id}", response_class=HTMLResponse)
def page_run_detail(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    results = db.query(Result).filter(Result.run_id == run_id).order_by(Result.id.asc()).all()
    latest_by_doc: dict[str, Result] = {}
    for r in results:
        latest_by_doc[r.document_id] = r
    results_with_effective = []
    for r in latest_by_doc.values():
        eff = get_result_with_effective(db, r.id)
        if eff:
            results_with_effective.append(eff)
    run_stats = get_run_field_stats(db, run_id)
    return templates.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "run": run,
            "results": results_with_effective,
            "run_stats": run_stats,
            "pass_threshold_pct": PASS_ACCURACY_THRESHOLD_PCT,
        },
    )


@web_router.get("/expected", response_class=HTMLResponse)
def page_expected(request: Request, db: Session = Depends(get_db)):
    docs = db.query(ExpectedDocument).order_by(ExpectedDocument.document_id).all()
    return templates.TemplateResponse("expected.html", {"request": request, "docs": docs})


@web_router.get("/upload-form", response_class=HTMLResponse)
def page_upload_form(request: Request):
    return templates.TemplateResponse("upload_form.html", {"request": request})


# ---- Saved ground truth (Extract & Edit) ----

@api_router.get("/saved-ground-truth")
def api_list_saved_ground_truth(include_fields: bool = False, db: Session = Depends(get_db)):
    """List saved ground truth docs. If include_fields=true, return full docs with fields for manage page."""
    rows = db.query(SavedGroundTruth).order_by(SavedGroundTruth.name.asc()).all()
    if not include_fields:
        return [{"id": r.id, "name": r.name, "document_id": r.document_id, "created_at": _dt_iso(r.created_at)} for r in rows]
    return [{"id": r.id, "name": r.name, "document_id": r.document_id, "fields": json.loads(r.fields_json), "created_at": _dt_iso(r.created_at)} for r in rows]


@api_router.post("/saved-ground-truth")
def api_create_saved_ground_truth(
    name: str = Form(...),
    fields_json: str = Form(...),
    document_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Save corrected fields from Extract & Edit. fields_json = JSON string of IDP-shaped fields."""
    import logging
    logging.getLogger(__name__).info("saved-ground-truth POST name=%r len(fields_json)=%s", name, len(fields_json) if fields_json else 0)
    try:
        fields = json.loads(fields_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid fields JSON: {e}")
    if not isinstance(fields, dict):
        raise HTTPException(400, "fields must be a JSON object")
    if set(fields.keys()) == {"fields"} and isinstance(fields.get("fields"), dict):
        fields = fields["fields"]
    doc = create_saved_ground_truth(db, name.strip(), fields, document_id.strip() if document_id else None)
    logging.getLogger(__name__).info("saved-ground-truth saved id=%s document_id=%s", doc.id, doc.document_id)
    return {"ok": True, "id": doc.id, "name": doc.name, "document_id": doc.document_id}


@api_router.get("/saved-ground-truth/{id}")
def api_get_saved_ground_truth(id: int, db: Session = Depends(get_db)):
    """Get one saved ground truth (for Load into Expected)."""
    doc = get_saved_ground_truth(db, id)
    if not doc:
        raise HTTPException(404, "Saved document not found")
    return doc


@api_router.delete("/saved-ground-truth/{id}")
def api_delete_saved_ground_truth(id: int, db: Session = Depends(get_db)):
    """Delete a saved ground truth document."""
    if not delete_saved_ground_truth(db, id):
        raise HTTPException(404, "Saved document not found")
    return {"ok": True}


@web_router.get("/extract-edit", response_class=HTMLResponse)
def page_extract_edit(request: Request):
    return templates.TemplateResponse("extract_edit.html", {"request": request})


@web_router.get("/saved-ground-truth", response_class=HTMLResponse)
def page_saved_ground_truth(request: Request):
    """List all saved ground truth (expected) forms with fields and delete."""
    return templates.TemplateResponse("saved_ground_truth.html", {"request": request})


# ---- AssistRx 9-document test suite ----

@api_router.get("/suite")
def api_suite_overview(db: Session = Depends(get_db)):
    return get_suite_overview(db)


@api_router.get("/suite/stats/{document_id}")
def api_document_aggregate_stats(document_id: str, db: Session = Depends(get_db)):
    """Average field % correct across all runs for a document (e.g. enrollment-vyvgart-1)."""
    return get_document_aggregate_stats(db, document_id)


@api_router.get("/suite/profiles/{profile_id}")
def api_get_profile(profile_id: str, db: Session = Depends(get_db)):
    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    gt = get_saved_ground_truth_by_document_id(db, profile.document_id)
    latest = get_latest_result_for_document(db, profile.document_id)
    aggregate = get_document_aggregate_stats(db, profile.document_id)
    return {**profile.to_dict(), "ground_truth": gt, "latest": latest, "aggregate": aggregate}


@api_router.post("/suite/profiles/{profile_id}/ground-truth")
def api_save_profile_ground_truth(
    profile_id: str,
    fields_json: str = Form(...),
    db: Session = Depends(get_db),
):
    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    try:
        fields = json.loads(fields_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")
    if not isinstance(fields, dict):
        raise HTTPException(400, "fields must be a JSON object")
    doc = save_profile_ground_truth(db, profile, fields)
    return {"ok": True, "id": doc.id, "document_id": doc.document_id, "name": doc.name}


@api_router.post("/suite/profiles/{profile_id}/run-test")
async def api_run_profile_test(
    profile_id: str,
    file: UploadFile | None = File(None),
    platform: str = Form("production"),
    region: str | None = Form(None),
    org_id: str | None = Form(None),
    action_id: str | None = Form(None),
    action_version: str | None = Form(None),
    client_id: str | None = Form(None),
    client_secret: str | None = Form(None),
    fields_json: str | None = Form(None),
    db: Session = Depends(get_db),
):
    logger.info("api_run_profile_test start profile_id=%s filename=%s", profile_id, file.filename if file else None)
    try:
        return await _api_run_profile_test_impl(
            profile_id,
            file,
            platform,
            region,
            org_id,
            action_id,
            action_version,
            client_id,
            client_secret,
            fields_json,
            db,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("api_run_profile_test unhandled error profile_id=%s", profile_id)
        return _attach_request_meta({
            "ok": False,
            "error": f"Server error during run test: {type(exc).__name__}: {exc}",
            "exception_type": type(exc).__name__,
        })


async def _api_run_profile_test_impl(
    profile_id: str,
    file: UploadFile | None,
    platform: str,
    region: str | None,
    org_id: str | None,
    action_id: str | None,
    action_version: str | None,
    client_id: str | None,
    client_secret: str | None,
    fields_json: str | None,
    db: Session,
):
    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    resolved, target_meta = resolve_idp_execution_params(
        db,
        document_id=profile.document_id,
        platform=platform,
        region=region,
        org_id=org_id,
        action_id=action_id,
        action_version=action_version,
        client_id=client_id,
        client_secret=client_secret,
    )
    for w in target_meta.get("warnings") or []:
        logger.warning("api_run_profile_test %s", w)
    _persist_idp_form_credentials(
        db,
        resolved["platform"],
        resolved["region"],
        resolved["org_id"],
        resolved["action_id"],
        resolved["action_version"],
        resolved["client_id"],
        resolved["client_secret"],
    )
    parsed_ui_fields: dict[str, Any] | None = None
    if fields_json is not None and fields_json.strip():
        try:
            parsed_ui_fields = json.loads(fields_json)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"Invalid fields_json: {e}")
        if not isinstance(parsed_ui_fields, dict):
            raise HTTPException(400, "fields_json must be a JSON object")

    if file is None:
        raise HTTPException(400, "Upload a document (PDF/image) to run the test.")

    content = await file.read()
    idp_result = await execute_idp_document_action(
        resolved["region"],
        resolved["org_id"],
        resolved["action_id"],
        resolved["action_version"],
        content,
        file.filename or "document.pdf",
        file.content_type,
        resolved["client_id"],
        resolved["client_secret"],
        resolved["platform"],
        None,
    )
    body = idp_result.get("body") or {}
    status = (idp_result.get("status") or "").upper()
    has_body = isinstance(body, dict) and len(body) > 0
    if not idp_result.get("ok") and not (
        has_body and status in ("SUCCEEDED", "MANUAL_VALIDATION_REQUIRED", "PARTIAL_SUCCESS")
    ):
        logger.warning(
            "api_run_profile_test IDP failed profile_id=%s error=%s http_status=%s",
            profile_id,
            idp_result.get("error"),
            idp_result.get("http_status"),
        )
        return _attach_request_meta({
            "ok": False,
            "error": idp_result.get("error") or "IDP extraction failed",
            "http_status": idp_result.get("http_status"),
            "executionId": idp_result.get("executionId"),
        })

    run, result, err = run_profile_comparison(
        db,
        profile,
        body if isinstance(body, dict) else {},
        ui_fields=parsed_ui_fields,
    )
    if err:
        extracted = extract_from_idp_body(profile, body)
        body_keys = list(body.keys())[:20] if isinstance(body, dict) else []
        fields_preview = []
        if isinstance(body, dict):
            raw_fields = body.get("fields")
            if isinstance(raw_fields, dict):
                fields_preview = list(raw_fields.keys())[:15]
        logger.warning(
            "api_run_profile_test compare failed profile_id=%s err=%s extracted=%s",
            profile_id,
            err,
            len(extracted),
        )
        return _attach_request_meta({
            "ok": False,
            "error": err,
            "extracted_field_count": len(extracted),
            "idp_has_fields": has_profile_field_values(extracted, profile),
            "idp_extraction_incomplete": idp_body_extraction_incomplete(body) if isinstance(body, dict) else True,
            "idp_body_keys": body_keys,
            "idp_field_keys": fields_preview,
        })
    eff = get_result_with_effective(db, result.id) if result else None
    pct = None
    meaningful_score = None
    if eff and eff.get("total_fields"):
        pct = round(100.0 * (eff.get("score") or 0) / eff["total_fields"], 1)
        meaningful_score = sum(
            1 for d in (eff.get("diff_details") or [])
            if d.get("match") and not d.get("sentinel_match")
        )
    null_actuals = 0
    if eff and eff.get("diff_details"):
        null_actuals = sum(1 for d in eff["diff_details"] if d.get("actual_raw") is None)
    extracted = extract_from_idp_body(profile, body)
    logger.info(
        "api_run_profile_test ok profile_id=%s run_id=%s score=%s/%s meaningful=%s",
        profile_id,
        run.id if run else None,
        eff.get("score") if eff else None,
        eff.get("total_fields") if eff else None,
        meaningful_score,
    )
    return _attach_request_meta({
        "ok": True,
        "run_id": run.id if run else None,
        "result_id": result.id if result else None,
        "document_id": profile.document_id,
        "score": eff.get("score") if eff else None,
        "meaningful_score": meaningful_score,
        "total_fields": eff.get("total_fields") if eff else None,
        "effective_pass": eff.get("effective_pass") if eff else None,
        "pct": pct,
        "meaningful_pct": round(100.0 * meaningful_score / eff["total_fields"], 1) if eff and eff.get("total_fields") and meaningful_score is not None else None,
        "extracted_field_count": len(extracted),
        "extracted_fields": gt_fields_for_extract_ui(body if isinstance(body, dict) else {}, profile),
        "null_actual_count": null_actuals,
        "idp_has_fields": has_profile_field_values(extracted, profile),
        "idp_extraction_incomplete": idp_body_extraction_incomplete(body) if isinstance(body, dict) else True,
        "idp_body": body if isinstance(body, dict) else None,
    })


@web_router.get("/suite", response_class=HTMLResponse)
def page_suite(request: Request, db: Session = Depends(get_db)):
    items = get_suite_overview(db)
    summary = get_suite_summary(db)
    categories = {"enrollment": [], "insurance": [], "copay": []}
    for item in items:
        cat = item.get("category", "enrollment")
        if cat in categories:
            categories[cat].append(item)
    return templates.TemplateResponse(
        "suite.html",
        {"request": request, "categories": categories, "items": items, "summary": summary},
    )


@web_router.get("/suite/{profile_id}", response_class=HTMLResponse)
def page_suite_document(request: Request, profile_id: str, db: Session = Depends(get_db)):
    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    gt = get_saved_ground_truth_by_document_id(db, profile.document_id)
    latest = get_latest_result_for_document(db, profile.document_id)
    aggregate = get_document_aggregate_stats(db, profile.document_id)
    gt_display_fields: dict[str, Any] = {}
    if gt and gt.get("fields"):
        from app.assistrx_extract import normalize_expected_for_profile

        flat = normalize_expected_for_profile(gt["fields"], profile)
        gt_display_fields = {k: {"value": v} for k, v in flat.items()}
    return templates.TemplateResponse(
        "suite_document.html",
        {
            "request": request,
            "profile": profile.to_dict(),
            "ground_truth": gt,
            "latest": latest,
            "aggregate": aggregate,
            "gt_fields_json": json.dumps(gt_display_fields),
            "has_ground_truth": gt is not None,
            "field_keys_json": json.dumps(profile.field_keys),
        },
    )


# ---- Stored IDP credentials (persist across refreshes and all sessions) ----

@api_router.get("/settings/idp-credentials")
def api_get_idp_credentials(db: Session = Depends(get_db)):
    """Return stored IDP credentials for pre-fill. Persists across refreshes and browser sessions."""
    try:
        creds = get_idp_credentials(db)
        if creds is None:
            return {}
        norm = normalize_stored_credentials(creds)
        plat = (norm.get("platform") or "production").strip().lower()
        bp = norm.get("by_platform") or {}
        pc = bp.get(plat) if isinstance(bp.get(plat), dict) else {}
        if pc.get("client_id"):
            norm["client_id"] = pc["client_id"]
        if pc.get("client_secret"):
            norm["client_secret"] = pc["client_secret"]
        return norm
    except Exception:
        return {}


@api_router.post("/settings/idp-credentials")
def api_set_idp_credentials(
    platform: str = Form("production"),
    region: str = Form(""),
    org_id: str = Form(""),
    action_id: str = Form(""),
    action_version: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save IDP credentials so they persist across refreshes and all browser sessions."""
    payload = {
        "platform": (platform or "production").strip(),
        "region": (region or "").strip(),
        "org_id": (org_id or "").strip(),
        "action_id": (action_id or "").strip(),
        "action_version": (action_version or "").strip(),
        "client_id": (client_id or "").strip(),
        "client_secret": (client_secret or "").strip(),
    }
    try:
        merge_idp_credentials_save(db, payload)
        return {"ok": True, "saved": True, "platform": payload["platform"]}
    except Exception as e:
        err_str = str(e).lower()
        if "no such table" in err_str or "operational" in err_str:
            init_db()
            try:
                merge_idp_credentials_save(db, payload)
                return {"ok": True, "saved": True, "platform": payload["platform"]}
            except Exception as e2:
                raise HTTPException(500, f"Failed to save credentials: {e2}")
        raise HTTPException(500, f"Failed to save credentials: {e}")

