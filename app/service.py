"""Business logic: expected data, runs, comparison, overrides.

Data retention (for maintainers)
--------------------------------
Runs and their results/overrides are removed only from:
  - delete_run() — explicit per-run delete (API DELETE /api/runs/{id}).
  - reset_metrics() — user action "Reset all stats" (POST /api/reset); backs up then deletes all runs.
  - restore_from_backup() — Undo after reset (POST /api/undo); deletes current runs/results/overrides
    then restores from that backup (one-time).

Saved ground truth (SavedGroundTruth) is deleted only via delete_saved_ground_truth()
(API DELETE /api/saved-ground-truth/{id}). Reset, run delete, and new runs never touch this table.

ExpectedDocument rows are upserted per document_id by load_expected / load_expected_bulk; there is no
delete-all or table drop for expected data in application code.

init_db() only creates tables if missing; it does not drop data. New features must not add silent
batch deletes or startup wipes of the above without an explicit, user-controlled action.
"""
import json
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.compare import compare_fields, field_accuracy_pct, passes_accuracy_threshold, PASS_ACCURACY_THRESHOLD_PCT, flatten_section_shaped_to_fields, normalize_body_fields
from app.models import ExpectedDocument, IdpCredentials, Result, ResultOverride, Run, ResetBackup, SavedGroundTruth
from app.assistrx_profiles import (
    DocumentProfile,
    canonical_suite_document_id,
    get_profile,
    list_profiles,
    profile_for_suite_document_id,
)
from app.assistrx_extract import (
    _is_flat_compare_map,
    extract_from_idp_body,
    ground_truth_for_compare,
    has_meaningful_field_values,
    has_profile_field_values,
    idp_body_has_meaningful_fields,
    is_idp_execution_or_wrapper,
    normalize_expected_for_profile,
    prepare_actual_for_compare,
    profile_for_document_id,
    resolve_actual_for_compare,
    _unwrap_ground_truth_fields,
)
from app.field_keys import canonicalize_field_map

def load_expected(db: Session, document_id: str, fields: dict[str, Any], field_types: Optional[dict[str, str]] = None) -> ExpectedDocument:
    """Upsert expected document."""
    profile = profile_for_document_id(document_id)
    if profile:
        fields = normalize_expected_for_profile(fields, profile)
        field_types = field_types or profile.field_types
    doc = db.query(ExpectedDocument).filter(ExpectedDocument.document_id == document_id).first()
    if doc:
        doc.fields_json = json.dumps(fields)
        doc.field_types_json = json.dumps(field_types) if field_types else None
    else:
        doc = ExpectedDocument(
            document_id=document_id,
            fields_json=json.dumps(fields),
            field_types_json=json.dumps(field_types) if field_types else None,
        )
        db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def load_expected_bulk(db: Session, documents: dict[str, dict[str, Any]], field_types_global: Optional[dict[str, str]] = None) -> int:
    """Load many expected documents. Returns count loaded."""
    for doc_id, fields in documents.items():
        load_expected(db, doc_id, fields, field_types_global)
    return len(documents)


def get_expected(db: Session, document_id: str) -> Optional[tuple[dict, dict]]:
    """Return (fields, field_types) or None. Field keys are canonicalized for compare."""
    doc = db.query(ExpectedDocument).filter(ExpectedDocument.document_id == document_id).first()
    if not doc:
        return None
    fields = json.loads(doc.fields_json)
    types = json.loads(doc.field_types_json) if doc.field_types_json else {}
    profile = profile_for_document_id(document_id)
    if profile:
        fields = normalize_expected_for_profile(fields, profile)
        if not types:
            types = profile.field_types or {}
    else:
        fields = canonicalize_field_map(_unwrap_ground_truth_fields(fields))
    return (fields, types)


def create_run(db: Session, label: Optional[str] = None) -> Run:
    r = Run(label=label)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


log = logging.getLogger(__name__)


def submit_result(db: Session, run_id: int, document_id: str, fields: dict[str, Any]) -> Optional[Result]:
    """Compare with expected, store result. Returns None if no expected for document_id."""
    profile = profile_for_document_id(document_id)
    if isinstance(fields, dict) and profile:
        raw_keys = list(fields.keys())[:20]
        if _is_flat_compare_map(fields):
            from app.assistrx_extract import _flatten_compare_map, _pick_keys

            fields = _pick_keys(_flatten_compare_map(fields), profile.field_keys) or _flatten_compare_map(fields)
        elif is_idp_execution_or_wrapper(fields):
            fields = extract_from_idp_body(profile, fields)
        else:
            fields = prepare_actual_for_compare(fields, profile)
        if not fields:
            log.warning(
                "submit_result empty actual document_id=%s raw_payload_keys=%s",
                document_id,
                raw_keys,
            )
    expected_data = get_expected(db, document_id)
    if not expected_data:
        return None
    expected, field_types = expected_data
    pass_, score, total, details = compare_fields(expected, fields, field_types)

    existing = (
        db.query(Result)
        .filter(Result.run_id == run_id, Result.document_id == document_id)
        .order_by(Result.id.desc())
        .first()
    )
    if existing:
        existing.pass_ = pass_
        existing.score = score
        existing.total_fields = total
        existing.diff_details = json.dumps(details, default=str)
        db.commit()
        db.refresh(existing)
        return existing

    result = Result(
        run_id=run_id,
        document_id=document_id,
        pass_=pass_,
        score=score,
        total_fields=total,
        diff_details=json.dumps(details, default=str),
    )
    db.add(result)
    db.commit()
    db.refresh(result)
    return result


def set_override(db: Session, result_id: int, field_name: str, override_pass: bool) -> ResultOverride:
    """Set or update override for a field on a result."""
    existing = db.query(ResultOverride).filter(
        ResultOverride.result_id == result_id,
        ResultOverride.field_name == field_name,
    ).first()
    if existing:
        existing.override_pass = override_pass
        db.commit()
        db.refresh(existing)
        return existing
    ov = ResultOverride(result_id=result_id, field_name=field_name, override_pass=override_pass)
    db.add(ov)
    db.commit()
    db.refresh(ov)
    return ov


def clear_override(db: Session, result_id: int, field_name: str) -> bool:
    """Remove override for a field. Returns True if one was removed."""
    n = db.query(ResultOverride).filter(
        ResultOverride.result_id == result_id,
        ResultOverride.field_name == field_name,
    ).delete()
    db.commit()
    return n > 0


def get_result_with_effective(db: Session, result_id: int) -> Optional[dict]:
    """Get result and compute effective pass/fail per field (override wins)."""
    result = db.query(Result).filter(Result.id == result_id).first()
    if not result:
        return None
    overrides = {o.field_name: o.override_pass for o in result.overrides}
    details = json.loads(result.diff_details) if result.diff_details else []
    effective_fields = []
    for d in details:
        fn = d["field"]
        base_match = d["match"]
        ov = overrides.get(fn)
        effective_match = ov if ov is not None else base_match
        effective_fields.append({**d, "override_pass": ov, "effective_match": effective_match})
    effective_score = sum(1 for f in effective_fields if f.get("effective_match"))
    total = result.total_fields or 0
    effective_pass = (
        passes_accuracy_threshold(effective_score, total)
        if total
        else result.pass_
    )
    effective_pct = field_accuracy_pct(effective_score, total) if total else None
    return {
        "id": result.id,
        "run_id": result.run_id,
        "document_id": result.document_id,
        "pass": result.pass_,
        "score": result.score,
        "effective_score": effective_score,
        "total_fields": total,
        "effective_pct": effective_pct,
        "pass_threshold_pct": PASS_ACCURACY_THRESHOLD_PCT,
        "diff_details": effective_fields,
        "overrides": overrides,
        "effective_pass": effective_pass,
    }


def effective_field_pct_for_result(db: Session, result_id: int) -> Optional[float]:
    """Field-level % correct for one result (includes manual overrides)."""
    eff = get_result_with_effective(db, result_id)
    if not eff or not eff.get("total_fields"):
        return None
    right = eff.get("effective_score")
    if right is None:
        right = sum(1 for d in eff.get("diff_details") or [] if d.get("effective_match"))
    return round(100.0 * right / eff["total_fields"], 1)


def _serialize_run(r: Run) -> dict:
    return {"id": r.id, "label": r.label, "created_at": r.created_at.isoformat() if r.created_at else None}


def _serialize_result(r: Result) -> dict:
    return {
        "id": r.id,
        "run_id": r.run_id,
        "document_id": r.document_id,
        "pass": r.pass_,
        "score": r.score,
        "total_fields": r.total_fields,
        "diff_details": r.diff_details,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _serialize_override(o: ResultOverride) -> dict:
    return {
        "id": o.id,
        "result_id": o.result_id,
        "field_name": o.field_name,
        "override_pass": o.override_pass,
    }


def save_reset_backup(db: Session) -> bool:
    """Save current runs, results, overrides to ResetBackup (id=1). Returns True if any data saved."""
    runs = db.query(Run).order_by(Run.id).all()
    if not runs:
        return False
    results = db.query(Result).order_by(Result.id).all()
    overrides = db.query(ResultOverride).order_by(ResultOverride.id).all()
    data = {
        "runs": [_serialize_run(r) for r in runs],
        "results": [_serialize_result(r) for r in results],
        "overrides": [_serialize_override(o) for o in overrides],
    }
    backup = db.query(ResetBackup).filter(ResetBackup.id == 1).first()
    if backup:
        backup.data_json = json.dumps(data)
        backup.created_at = datetime.utcnow()
    else:
        backup = ResetBackup(id=1, data_json=json.dumps(data))
        db.add(backup)
    db.commit()
    return True


def reset_metrics(db: Session) -> bool:
    """Save backup (for Undo), then delete all runs (cascades to results and overrides).

    Does not delete SavedGroundTruth or ExpectedDocument rows.
    Returns True if backup was saved."""
    had_data = save_reset_backup(db)
    db.query(Run).delete()
    db.commit()
    return had_data


def restore_from_backup(db: Session) -> bool:
    """Restore runs/results/overrides from last Reset backup. Returns True if restored."""
    backup = db.query(ResetBackup).filter(ResetBackup.id == 1).first()
    if not backup or not backup.data_json:
        return False
    data = json.loads(backup.data_json)
    runs_data = data.get("runs") or []
    results_data = data.get("results") or []
    overrides_data = data.get("overrides") or []
    if not runs_data:
        return False
    # Delete current state (order matters for FK)
    db.query(ResultOverride).delete()
    db.query(Result).delete()
    db.query(Run).delete()
    db.commit()
    # Restore with original IDs
    for row in runs_data:
        r = Run(
            id=row["id"],
            label=row.get("label"),
            created_at=datetime.fromisoformat(row["created_at"]) if row.get("created_at") else None,
        )
        db.add(r)
    db.flush()
    for row in results_data:
        r = Result(
            id=row["id"],
            run_id=row["run_id"],
            document_id=row["document_id"],
            pass_=row["pass"],
            score=row.get("score"),
            total_fields=row.get("total_fields"),
            diff_details=row.get("diff_details"),
            created_at=datetime.fromisoformat(row["created_at"]) if row.get("created_at") else None,
        )
        db.add(r)
    db.flush()
    for row in overrides_data:
        o = ResultOverride(
            id=row["id"],
            result_id=row["result_id"],
            field_name=row["field_name"],
            override_pass=row["override_pass"],
        )
        db.add(o)
    db.commit()
    # Clear backup so Undo is one-time
    backup = db.query(ResetBackup).filter(ResetBackup.id == 1).first()
    if backup:
        backup.data_json = ""
    db.commit()
    return True


def has_undo_available(db: Session) -> bool:
    """True if a reset backup exists and has data."""
    backup = db.query(ResetBackup).filter(ResetBackup.id == 1).first()
    if not backup or not backup.data_json:
        return False
    try:
        data = json.loads(backup.data_json)
        return bool(data.get("runs"))
    except Exception:
        return False


def get_run_success_rate(db: Session, run_id: int) -> Optional[float]:
    """Return percentage (0-100) of documents that passed (effective), or None if no results."""
    results = db.query(Result).filter(Result.run_id == run_id).all()
    if not results:
        return None
    passed = 0
    for r in results:
        eff = get_result_with_effective(db, r.id)
        if eff and eff.get("effective_pass"):
            passed += 1
    return round(100.0 * passed / len(results), 1)


def create_run_from_json(
    db: Session,
    expected_docs: dict[str, dict[str, Any]],
    idp_docs: dict[str, dict[str, Any]],
    label: Optional[str] = None,
    idp_bodies: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[Run, int, list[str]]:
    """
    Create a run from two JSON objects (expected and IDP), both keyed by document_id.
    Loads expected, submits each IDP result. Returns (run, count_submitted, warnings).
    Manual runs always proceed even when IDP values are empty (compare shows null actuals).
    """
    run = create_run(db, label)
    load_expected_bulk(db, expected_docs)
    count = 0
    warnings: list[str] = []
    bodies = idp_bodies or {}
    doc_ids = set(expected_docs.keys()) | set(idp_docs.keys()) | set(bodies.keys())
    for doc_id in doc_ids:
        profile = profile_for_document_id(doc_id)
        if profile and profile.document_id != doc_id:
            log.info(
                "create_run_from_json resolved profile %s for document_id=%s",
                profile.document_id,
                doc_id,
            )
        fields = idp_docs.get(doc_id) if isinstance(idp_docs.get(doc_id), dict) else {}
        prepared: dict[str, Any] = {}
        raw_body = bodies.get(doc_id)
        if profile and isinstance(raw_body, dict) and raw_body:
            prepared = extract_from_idp_body(profile, raw_body)
            n_keys = len(profile.field_keys or [])
            log.info(
                "create_run_from_json extract document_id=%s field_count=%s/%s",
                doc_id,
                len(prepared),
                n_keys,
            )
            if n_keys and len(prepared) < max(16, n_keys // 3):
                warnings.append(
                    f"{doc_id}: only {len(prepared)}/{n_keys} profile fields extracted from IDP body — "
                    "response may be partial (check staging action version, or open raw JSON in Send to IDP)."
                )
        elif profile:
            prepared = prepare_actual_for_compare(fields if isinstance(fields, dict) else {}, profile)
        elif isinstance(fields, dict) and fields:
            flat = _unwrap_ground_truth_fields(fields)
            prepared = {k: v for k, v in flat.items() if v is not None}
        if not prepared:
            warnings.append(
                f"{doc_id}: no IDP field values (compare will show empty actuals — "
                "check credentials/action or paste a fuller IDP JSON body)."
            )
        if submit_result(db, run.id, doc_id, prepared) is not None:
            count += 1
    if count == 0:
        delete_run(db, run.id)
        return (None, 0, warnings or ["No results submitted — document_id in expected/idp may not match."])
    return (run, count, warnings)


def get_run_field_stats(db: Session, run_id: int) -> dict[str, Any]:
    """Return total_fields, right, wrong, pct for a run (one result per document_id, latest wins)."""
    results = db.query(Result).filter(Result.run_id == run_id).order_by(Result.id.asc()).all()
    latest_by_doc: dict[str, Result] = {}
    for r in results:
        latest_by_doc[r.document_id] = r
    total = 0
    right = 0
    meaningful_right = 0
    for r in latest_by_doc.values():
        eff = get_result_with_effective(db, r.id)
        if not eff or not eff.get("diff_details"):
            continue
        for d in eff["diff_details"]:
            total += 1
            if d.get("effective_match"):
                right += 1
                if not d.get("sentinel_match"):
                    meaningful_right += 1
    wrong = total - right
    pct = round(100.0 * right / total, 1) if total else 0.0
    meaningful_pct = round(100.0 * meaningful_right / total, 1) if total else 0.0
    return {
        "total_fields": total,
        "right": right,
        "wrong": wrong,
        "pct": pct,
        "meaningful_right": meaningful_right,
        "meaningful_pct": meaningful_pct,
        "document_count": len(latest_by_doc),
        "pass_threshold_pct": PASS_ACCURACY_THRESHOLD_PCT,
        "run_pass": passes_accuracy_threshold(right, total) if total else False,
    }


def get_all_time_stats(db: Session) -> dict[str, Any]:
    """Running tally across all runs: total fields, right, wrong, % correct."""
    runs = db.query(Run).all()
    total = 0
    right = 0
    for run in runs:
        stats = get_run_field_stats(db, run.id)
        total += stats["total_fields"]
        right += stats["right"]
    wrong = total - right
    pct = round(100.0 * right / total, 1) if total else None
    return {"total_fields": total, "right": right, "wrong": wrong, "pct": pct}


def update_run_label(db: Session, run_id: int, label: str) -> Optional[Run]:
    """Set run label; empty/whitespace clears it. Returns None if run not found."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        return None
    s = (label or "").strip()
    run.label = s[:255] if s else None
    db.commit()
    db.refresh(run)
    return run


def delete_run(db: Session, run_id: int) -> bool:
    """Delete one run (cascades to results and overrides). Returns True if deleted."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        return False
    db.delete(run)
    db.commit()
    return True


# ---- Saved ground truth (from Extract & Edit) ----

def _slug(name: str) -> str:
    """Simple slug for document_id from name."""
    s = "".join(c if c.isalnum() or c in "._-" else "_" for c in (name or "").strip())
    return s[:500] or "unnamed"


def list_saved_ground_truth(db: Session) -> list[dict[str, Any]]:
    """List all saved ground truth docs for dropdown."""
    rows = db.query(SavedGroundTruth).order_by(SavedGroundTruth.name.asc()).all()
    return [{"id": r.id, "name": r.name, "document_id": r.document_id, "created_at": r.created_at.isoformat()} for r in rows]


def create_saved_ground_truth(db: Session, name: str, fields: dict[str, Any], document_id: Optional[str] = None) -> SavedGroundTruth:
    """Save a ground truth document. document_id defaults to slug of name (unique)."""
    doc_id = (document_id or _slug(name)).strip() or "unnamed"
    profile = profile_for_document_id(doc_id)
    if profile:
        flat = normalize_expected_for_profile(fields, profile)
        fields = {k: {"value": v} for k, v in flat.items()}
    existing = db.query(SavedGroundTruth).filter(SavedGroundTruth.document_id == doc_id).first()
    if existing:
        existing.name = name
        existing.fields_json = json.dumps(fields)
        db.commit()
        db.refresh(existing)
        return existing
    doc = SavedGroundTruth(name=name, document_id=doc_id, fields_json=json.dumps(fields))
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def get_saved_ground_truth(db: Session, id: int) -> Optional[dict[str, Any]]:
    """Get one saved ground truth by id."""
    row = db.query(SavedGroundTruth).filter(SavedGroundTruth.id == id).first()
    if not row:
        return None
    return {"id": row.id, "name": row.name, "document_id": row.document_id, "fields": json.loads(row.fields_json), "created_at": row.created_at.isoformat()}


def delete_saved_ground_truth(db: Session, id: int) -> bool:
    """Delete one saved ground truth by id (only API/UI delete path). Returns True if deleted."""
    row = db.query(SavedGroundTruth).filter(SavedGroundTruth.id == id).first()
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


# ---- Stored IDP credentials (persist across refreshes and sessions) ----

def get_idp_credentials(db: Session) -> Optional[dict[str, Any]]:
    """Get stored IDP credentials (single row). Returns None if none stored."""
    row = db.query(IdpCredentials).filter(IdpCredentials.id == 1).first()
    if not row or not row.credentials_json:
        return None
    return json.loads(row.credentials_json)


def set_idp_credentials(db: Session, credentials: dict[str, Any]) -> None:
    """Store IDP credentials (upsert single row)."""
    row = db.query(IdpCredentials).filter(IdpCredentials.id == 1).first()
    payload = json.dumps(credentials)
    if row:
        row.credentials_json = payload
    else:
        row = IdpCredentials(id=1, credentials_json=payload)
        db.add(row)
    db.commit()


def normalize_stored_credentials(stored: dict[str, Any]) -> dict[str, Any]:
    """Ensure by_platform map exists; migrate legacy flat client_id into that platform bucket."""
    out = dict(stored)
    bp = out.get("by_platform")
    if not isinstance(bp, dict):
        bp = {}
    legacy_plat = (out.get("platform") or "staging").strip().lower() or "staging"
    legacy_cid = (out.get("client_id") or "").strip()
    legacy_secret = (out.get("client_secret") or "").strip()
    if legacy_cid and legacy_plat not in bp:
        bp[legacy_plat] = {"client_id": legacy_cid, "client_secret": legacy_secret}
    out["by_platform"] = bp
    return out


def merge_idp_credentials_save(db: Session, new_fields: dict[str, Any]) -> None:
    """Upsert stored credentials; OAuth secrets are kept per platform (production vs staging)."""
    existing = normalize_stored_credentials(get_idp_credentials(db) or {})
    plat = (new_fields.get("platform") or "production").strip().lower() or "production"
    cid = (new_fields.get("client_id") or "").strip()
    secret = (new_fields.get("client_secret") or "").strip()
    bp = dict(existing.get("by_platform") or {})
    if cid and secret:
        bp[plat] = {"client_id": cid, "client_secret": secret}
    merged: dict[str, Any] = {
        **existing,
        "platform": plat,
        "region": (new_fields.get("region") or existing.get("region") or "").strip(),
        "org_id": (new_fields.get("org_id") or existing.get("org_id") or "").strip(),
        "action_id": (new_fields.get("action_id") or existing.get("action_id") or "").strip(),
        "action_version": (new_fields.get("action_version") or existing.get("action_version") or "").strip(),
        "by_platform": bp,
    }
    if cid:
        merged["client_id"] = cid
    if secret:
        merged["client_secret"] = secret
    set_idp_credentials(db, merged)


def oauth_credentials_for_platform(
    stored: dict[str, Any],
    platform: str,
    form_client_id: str | None = None,
    form_client_secret: str | None = None,
) -> tuple[str, str]:
    """Connected App client_id/secret for the given Anypoint platform."""
    from app.idp_client import IDP_CLIENT_ID, IDP_CLIENT_SECRET

    plat = (platform or "production").strip().lower() or "production"

    def _s(val: str | None) -> str:
        return (val or "").strip()

    norm = normalize_stored_credentials(stored)
    bp = norm.get("by_platform") or {}
    pc = bp.get(plat) if isinstance(bp.get(plat), dict) else {}
    cid = _s(form_client_id) or _s(pc.get("client_id"))
    secret = _s(form_client_secret) or _s(pc.get("client_secret"))
    if not cid and plat == "production":
        cid = IDP_CLIENT_ID
        secret = IDP_CLIENT_SECRET
    return cid, secret


def resolve_idp_execution_params(
    db: Session,
    *,
    document_id: str | None = None,
    platform: str | None = None,
    region: str | None = None,
    org_id: str | None = None,
    action_id: str | None = None,
    action_version: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """
    Resolve IDP execute target. When document_id maps to a suite profile, profile
    platform/org/action/version win over saved global credentials (fixes New Run
    using staging 1.1.0 while ground truth came from production enrollment).
    """
    stored = get_idp_credentials(db) or {}
    doc_key = (document_id or "").strip()
    profile = (
        profile_for_document_id(doc_key)
        if doc_key and doc_key.lower() != "default"
        else None
    )
    warnings: list[str] = []

    def _s(val: str | None) -> str:
        return (val or "").strip()

    if profile:
        target_platform = (_s(platform) or profile.platform or "production").lower()
        oauth_cid, oauth_secret = oauth_credentials_for_platform(
            stored, target_platform, client_id, client_secret
        )
        resolved = {
            "platform": target_platform,
            "region": _s(region) or profile.region,
            "org_id": _s(org_id) or profile.org_id,
            "action_id": _s(action_id) or profile.action_id,
            "action_version": _s(action_version) or profile.action_version,
            "client_id": oauth_cid,
            "client_secret": oauth_secret,
        }
        source = "profile"
        sp = (stored.get("platform") or "").strip().lower()
        if stored and (
            sp != resolved["platform"]
            or (stored.get("org_id") or "").strip() != resolved["org_id"]
            or (stored.get("action_id") or "").strip() != resolved["action_id"]
            or (stored.get("action_version") or "").strip() != resolved["action_version"]
        ):
            warnings.append(
                f"IDP target from suite profile {profile.document_id} "
                f"({resolved['platform']} action {resolved['action_version']}), "
                f"not saved credentials ({stored.get('platform')} {stored.get('action_version')})."
            )
    else:
        target_platform = (_s(platform) or _s(stored.get("platform")) or "production").lower()
        oauth_cid, oauth_secret = oauth_credentials_for_platform(
            stored, target_platform, client_id, client_secret
        )
        resolved = {
            "platform": target_platform,
            "region": _s(region) or _s(stored.get("region")),
            "org_id": _s(org_id) or _s(stored.get("org_id")),
            "action_id": _s(action_id) or _s(stored.get("action_id")),
            "action_version": _s(action_version) or _s(stored.get("action_version")),
            "client_id": oauth_cid,
            "client_secret": oauth_secret,
        }
        source = "stored" if stored and not any(_s(x) for x in (platform, region, org_id, action_id, action_version)) else "form"

    norm = normalize_stored_credentials(stored)
    bp = norm.get("by_platform") or {}
    platforms_with_oauth = sorted(
        p for p, v in bp.items()
        if isinstance(v, dict) and (v.get("client_id") or "").strip() and (v.get("client_secret") or "").strip()
    )
    if not resolved["client_id"] or not resolved["client_secret"]:
        warnings.append(
            f"No Connected App saved for platform «{resolved['platform']}». "
            "On Extract & Edit set Platform to Production (US), enter production Client ID/Secret, "
            "and click Save credentials — staging secrets do not work against production IDP."
        )
    elif resolved["platform"] == "production" and "production" not in platforms_with_oauth and "staging" in platforms_with_oauth:
        warnings.append(
            "Using staging OAuth credentials for a production IDP call — likely to fail with 401. "
            "Save production Connected App credentials (Extract & Edit → Platform = Production → Save credentials)."
        )

    meta: dict[str, Any] = {
        "source": source,
        "document_id": doc_key or None,
        "profile_id": profile.id if profile else None,
        "warnings": warnings,
        "target": {
            "platform": resolved["platform"],
            "region": resolved["region"],
            "org_id": resolved["org_id"],
            "action_id": resolved["action_id"],
            "action_version": resolved["action_version"],
        },
        "oauth": {
            "needed_platform": resolved["platform"],
            "platforms_with_secrets": platforms_with_oauth,
            "ready": bool(resolved["client_id"] and resolved["client_secret"]),
            "client_id_prefix": (resolved["client_id"][:8] + "…") if len(resolved["client_id"]) >= 8 else None,
        },
    }
    return resolved, meta


# ---- AssistRx 9-document test suite ----

def _linked_document_ids(db: Session, document_id: str) -> list[str]:
    """All document_id values that refer to the same suite document (profile + saved aliases)."""
    key = (document_id or "").strip()
    if not key:
        return []
    ids: set[str] = {key}
    canon = canonical_suite_document_id(key)
    if canon:
        ids.add(canon)
    profile = profile_for_suite_document_id(key)
    if profile:
        ids.add(profile.document_id)
    target = profile.document_id if profile else canon
    if target:
        for row in db.query(SavedGroundTruth).filter(SavedGroundTruth.document_id.isnot(None)).all():
            rid = (row.document_id or "").strip()
            if rid and canonical_suite_document_id(rid) == target:
                ids.add(rid)
    return sorted(ids)


def get_saved_ground_truth_by_document_id(db: Session, document_id: str) -> Optional[dict[str, Any]]:
    for doc_id in _linked_document_ids(db, document_id):
        row = db.query(SavedGroundTruth).filter(SavedGroundTruth.document_id == doc_id).first()
        if row:
            return {
                "id": row.id,
                "name": row.name,
                "document_id": row.document_id,
                "fields": json.loads(row.fields_json),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
    return None


def get_latest_result_for_document(db: Session, document_id: str) -> Optional[dict[str, Any]]:
    """Most recent compare result for a document_id across all runs."""
    doc_ids = _linked_document_ids(db, document_id) or [document_id]
    result = (
        db.query(Result)
        .filter(Result.document_id.in_(doc_ids))
        .order_by(Result.created_at.desc(), Result.id.desc())
        .first()
    )
    if not result:
        return None
    eff = get_result_with_effective(db, result.id)
    if not eff:
        return None
    pct = effective_field_pct_for_result(db, result.id)
    stats = {
        "score": eff.get("effective_score") if eff.get("effective_score") is not None else eff.get("score"),
        "total_fields": eff.get("total_fields"),
        "pass": eff.get("effective_pass"),
        "pct": pct,
        "pass_threshold_pct": PASS_ACCURACY_THRESHOLD_PCT,
    }
    stats["run_id"] = result.run_id
    stats["result_id"] = result.id
    stats["created_at"] = result.created_at.isoformat() if result.created_at else None
    return stats


def get_document_aggregate_stats(db: Session, document_id: str) -> dict[str, Any]:
    """
    Average field-level % correct across all runs for this document_id.
    One score per run (latest result for that doc in the run if duplicated).
    """
    doc_ids = _linked_document_ids(db, document_id) or [document_id]
    results = (
        db.query(Result)
        .filter(Result.document_id.in_(doc_ids))
        .order_by(Result.run_id.asc(), Result.id.asc())
        .all()
    )
    latest_per_run: dict[int, Result] = {}
    for r in results:
        latest_per_run[r.run_id] = r

    run_rows: list[dict[str, Any]] = []
    for run_id, result in sorted(latest_per_run.items()):
        pct = effective_field_pct_for_result(db, result.id)
        if pct is None:
            continue
        eff = get_result_with_effective(db, result.id)
        run_rows.append(
            {
                "run_id": run_id,
                "result_id": result.id,
                "pct": pct,
                "score": eff.get("effective_score") if eff else result.score,
                "total_fields": result.total_fields,
                "created_at": result.created_at.isoformat() if result.created_at else None,
            }
        )

    run_rows.sort(key=lambda x: x.get("created_at") or "")
    pcts = [row["pct"] for row in run_rows]
    if not pcts:
        return {
            "document_id": document_id,
            "run_count": 0,
            "avg_pct": None,
            "min_pct": None,
            "max_pct": None,
            "last_pct": None,
            "total_runs_with_pct": 0,
            "pass_count": 0,
            "fail_count": 0,
            "pass_threshold_pct": PASS_ACCURACY_THRESHOLD_PCT,
            "avg_passes": None,
        }

    pass_count = sum(1 for p in pcts if p >= PASS_ACCURACY_THRESHOLD_PCT)
    return {
        "document_id": document_id,
        "run_count": len(pcts),
        "avg_pct": round(sum(pcts) / len(pcts), 1),
        "min_pct": min(pcts),
        "max_pct": max(pcts),
        "last_pct": pcts[-1],
        "total_runs_with_pct": len(pcts),
        "pass_count": pass_count,
        "fail_count": len(pcts) - pass_count,
        "pass_threshold_pct": PASS_ACCURACY_THRESHOLD_PCT,
        "avg_passes": pass_count >= (len(pcts) / 2),
    }


def get_suite_overview(db: Session) -> list[dict[str, Any]]:
    """All suite profiles with ground-truth status, latest score, and cross-run average."""
    out: list[dict[str, Any]] = []
    for profile in list_profiles():
        gt = get_saved_ground_truth_by_document_id(db, profile.document_id)
        latest = get_latest_result_for_document(db, profile.document_id)
        aggregate = get_document_aggregate_stats(db, profile.document_id)
        out.append({
            **profile.to_dict(),
            "has_ground_truth": gt is not None,
            "ground_truth_name": gt["name"] if gt else None,
            "latest": latest,
            "aggregate": aggregate,
        })
    return out


def get_suite_summary(db: Session) -> dict[str, Any]:
    """Aggregate stats for the suite dashboard."""
    items = get_suite_overview(db)
    with_gt = sum(1 for i in items if i.get("has_ground_truth"))
    tested = [i for i in items if i.get("latest") and i["latest"].get("pct") is not None]
    pcts = [i["latest"]["pct"] for i in tested]
    avg_last_pct = round(sum(pcts) / len(pcts), 1) if pcts else None

    with_runs = [i for i in items if (i.get("aggregate") or {}).get("avg_pct") is not None]
    avg_run_pcts = [i["aggregate"]["avg_pct"] for i in with_runs]
    average_run_avg_pct = round(sum(avg_run_pcts) / len(avg_run_pcts), 1) if avg_run_pcts else None
    total_run_count = sum((i.get("aggregate") or {}).get("run_count") or 0 for i in items)

    category_avg: dict[str, Optional[float]] = {}
    for cat in ("enrollment", "insurance", "copay"):
        cat_pcts = [
            i["aggregate"]["avg_pct"]
            for i in items
            if i.get("category") == cat and (i.get("aggregate") or {}).get("avg_pct") is not None
        ]
        category_avg[cat] = round(sum(cat_pcts) / len(cat_pcts), 1) if cat_pcts else None

    gt_with_runs = [i for i in items if i.get("has_ground_truth") and (i.get("aggregate") or {}).get("avg_pct") is not None]
    gt_avg_pcts = [i["aggregate"]["avg_pct"] for i in gt_with_runs]
    ground_truth_avg_pct = round(sum(gt_avg_pcts) / len(gt_avg_pcts), 1) if gt_avg_pcts else None

    return {
        "total_documents": len(items),
        "ground_truth_count": with_gt,
        "tested_count": len(tested),
        "average_last_pct": avg_last_pct,
        "documents_with_runs": len(with_runs),
        "total_run_count": total_run_count,
        "average_run_avg_pct": average_run_avg_pct,
        "ground_truth_avg_pct": ground_truth_avg_pct,
        "category_avg_pct": category_avg,
        "pass_threshold_pct": PASS_ACCURACY_THRESHOLD_PCT,
    }


def resync_all_expected_from_ground_truth(db: Session) -> int:
    """Rebuild expected_documents from saved ground truth using canonical field keys."""
    from app.assistrx_profiles import PROFILES_BY_ID

    count = 0
    rows = db.query(SavedGroundTruth).all()
    for row in rows:
        profile = PROFILES_BY_ID.get(row.document_id)
        if not profile:
            # document_id may be enrollment-vyvgart-1 matching profile.document_id
            for p in PROFILES_BY_ID.values():
                if p.document_id == row.document_id:
                    profile = p
                    break
        if not profile:
            continue
        fields = json.loads(row.fields_json)
        expected_flat = ground_truth_for_compare(fields, profile)
        if expected_flat:
            canonical_gt = {k: {"value": v} for k, v in expected_flat.items()}
            row.fields_json = json.dumps(canonical_gt)
            row.name = row.name  # touch row for commit
            load_expected(db, profile.document_id, expected_flat, profile.field_types)
            count += 1
    db.commit()
    return count


def save_profile_ground_truth(
    db: Session,
    profile: DocumentProfile,
    fields: dict[str, Any],
) -> SavedGroundTruth:
    """Save or update ground truth for a suite profile; sync expected_documents for compare."""
    expected_flat = ground_truth_for_compare(fields, profile)
    # Persist canonical keys so UI + compare stay aligned
    canonical_gt = {k: {"value": v} for k, v in expected_flat.items()}
    doc = create_saved_ground_truth(db, profile.name, canonical_gt, profile.document_id)
    load_expected(db, profile.document_id, expected_flat, profile.field_types)
    return doc


def run_profile_comparison(
    db: Session,
    profile: DocumentProfile,
    idp_body: Optional[dict[str, Any]] = None,
    label: Optional[str] = None,
    ui_fields: Optional[dict[str, Any]] = None,
) -> tuple[Optional[Run], Optional[Result], Optional[str]]:
    """
    Compare saved ground truth vs IDP actual for one profile.
    When ui_fields is provided ({field: {value}} from suite Extract UI / gatherFields),
    uses the same ground_truth_for_compare path as saving ground truth.
    Otherwise parses idp_body with idp_body_to_gt_field_shape (same as getFieldsFromBody).
    Returns (run, result, error_message).
    """
    gt = get_saved_ground_truth_by_document_id(db, profile.document_id)
    if not gt:
        return None, None, "No ground truth saved for this document. Extract, correct, and save first."

    expected = ground_truth_for_compare(gt["fields"], profile)
    if not expected:
        return None, None, "Ground truth has no fields to compare."

    actual = resolve_actual_for_compare(profile, idp_body, ui_fields)
    if not actual:
        return None, None, "No IDP fields or response body to compare."

    if not has_profile_field_values(actual, profile):
        return None, None, (
            "No enrollment field values in the IDP response (only classification or empty). "
            "Wait for extraction to finish, click Extract from IDP on the suite page, then Run test — "
            "or run test again after the execution includes prompts/fields/Master_Prompt data."
        )

    run_label = label or f"{profile.name} test"
    run = create_run(db, run_label)
    load_expected(db, profile.document_id, expected, profile.field_types)
    result = submit_result(db, run.id, profile.document_id, actual)
    if result is None:
        return run, None, "Comparison failed."
    return run, result, None
