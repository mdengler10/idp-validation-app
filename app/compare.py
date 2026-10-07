"""Compare expected vs actual using fuzzy normalization (REQ-1). Supports nested JSON."""
import json
from typing import Any, Optional

from app.field_keys import canonical_field_key, canonicalize_field_map, is_idp_field_wrapper
from app.normalize import normalize_value, _is_blank_sentinel


def flatten_section_shaped_to_fields(obj: Any, prefix: str = "") -> dict[str, Any]:
    """
    Flatten a section-shaped dict (e.g. {"section_1": {"a": "N", "b": "Y"}, "section_2": {...}})
    into a single level with dot keys and {"value": x} leaves. Unwraps {"value": <nested object>}
    so we recurse into the inner object instead of creating a single "fields.fields" key.
    """
    if obj is None:
        return {}
    if isinstance(obj, (str, int, float, bool)):
        return {prefix: {"value": obj}} if prefix else {}
    if isinstance(obj, list):
        return {prefix: {"value": obj}} if prefix else {}
    if not isinstance(obj, dict):
        return {prefix: {"value": obj}} if prefix else {}
    keys = list(obj.keys())
    if keys == ["value"]:
        inner = obj["value"]
        if (
            inner is not None
            and isinstance(inner, dict)
            and not (len(inner) == 1 and "value" in inner)
        ):
            return flatten_section_shaped_to_fields(inner, prefix)
        return {prefix: {"value": inner}} if prefix else {}
    out: dict[str, Any] = {}
    for k in keys:
        v = obj[k]
        p = f"{prefix}.{k}" if prefix else k
        if v is not None and isinstance(v, dict) and not is_idp_field_wrapper(v) and not (
            len(v) == 1 and "value" in v
        ):
            out.update(flatten_section_shaped_to_fields(v, p))
        elif (
            isinstance(v, dict)
            and len(v) == 1
            and "value" in v
            and isinstance(v.get("value"), dict)
            and v["value"] is not None
            and not (len(v["value"]) == 1 and "value" in v["value"])
        ):
            out.update(flatten_section_shaped_to_fields(v["value"], p))
        else:
            out[p] = v if isinstance(v, dict) and "value" in v else {"value": v}
    return out


def _is_section_shaped(obj: Any) -> bool:
    """True if obj is a dict and all values are dicts that aren't simple {value: x} wrappers."""
    if not obj or not isinstance(obj, dict):
        return False
    keys = list(obj.keys())
    if not keys:
        return False
    return all(
        isinstance(obj.get(k), dict) and not (
            len(obj[k]) == 1 and "value" in obj[k]
        )
        for k in keys
    )


def _parse_json_strings(obj: Any) -> Any:
    """Recursively replace JSON-looking strings with parsed objects."""
    if isinstance(obj, str) and obj.strip().startswith("{"):
        try:
            parsed = json.loads(obj)
            if isinstance(parsed, dict):
                return _parse_json_strings(parsed)
            return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    if isinstance(obj, dict):
        return {k: _parse_json_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_parse_json_strings(item) for item in obj]
    return obj


def normalize_body_fields(body: dict[str, Any]) -> dict[str, Any]:
    """
    Ensure body["fields"] is a flat map (one entry per leaf). If the IDP returns section-shaped
    data under body.fields, body.result.fields, or at body top level, flatten and set body["fields"].
    Parses any JSON string values so "{\"section_1\": ...}" becomes a dict before flattening.
    """
    if not body or not isinstance(body, dict):
        return body
    body = _parse_json_strings(body)
    if not isinstance(body, dict):
        return body
    # Prefer body.result.fields or body.execution.result.fields when body.fields is missing
    fields = body.get("fields")
    if fields is None or not isinstance(fields, dict):
        for path in ("result", "execution"):
            node = body.get(path)
            if isinstance(node, dict) and isinstance(node.get("fields"), dict):
                fields = node.get("fields")
                break
        if fields is None and isinstance(body.get("execution"), dict):
            exec_result = body["execution"].get("result")
            if isinstance(exec_result, dict) and isinstance(exec_result.get("fields"), dict):
                fields = exec_result.get("fields")
    if fields is not None and isinstance(fields, dict) and _is_section_shaped(fields):
        body = {**body, "fields": flatten_section_shaped_to_fields(fields)}
        return body
    # Body might be the section map at top level (no "fields" key)
    keys = list(body.keys())
    meta = {"status", "id", "executionId", "execution", "documentName", "documentId", "result", "tables", "extractionResult", "extractionResults"}
    non_meta = [k for k in keys if k not in meta]
    if non_meta and all(
        isinstance(body.get(k), dict) and not (
            len(body[k]) == 1 and "value" in body[k]
        )
        for k in non_meta
    ):
        section_like = {k: body[k] for k in non_meta}
        flat = flatten_section_shaped_to_fields(section_like)
        body = {**{k: body[k] for k in keys if k in meta}, "fields": flat}
    return body


def _get_value_by_path(obj: Any, path: str) -> Any:
    """
    Get a value from obj by dot path (e.g. 'state' or 'address.city').
    Unwraps {'value': x} at each level so IDP-shaped actual is found.
    If path starts with 'fields.' but obj has no 'fields' key, try path without prefix
    (expected stored as {fields:{...}} flattens to fields.xxx; actual from IDP is often flat).
    Returns None if path not present.
    """
    if obj is None or path is None:
        return None
    if not isinstance(obj, dict):
        return None
    # Try full path first
    val = _get_value_by_path_inner(obj, path)
    if val is not None:
        return val
    # Expected may be stored as { "fields": { "address": "..." } } -> path "fields.address"
    # while actual from IDP is flat { "address": "..." }. Try without "fields." prefix.
    if path.startswith("fields.") and "fields" not in obj:
        val = _get_value_by_path_inner(obj, path[7:])  # len("fields.") == 7
    return val


def _get_value_by_path_inner(obj: Any, path: str) -> Any:
    if obj is None or path is None or not isinstance(obj, dict):
        return None
    parts = path.split(".", 1)
    key = parts[0]
    rest = parts[1] if len(parts) > 1 else None
    val = obj.get(key)
    if val is not None and isinstance(val, dict) and set(val.keys()) == {"value"}:
        val = val["value"]
    if rest:
        return _get_value_by_path_inner(val, rest)
    return val


def _unwrap_value_dicts(obj: Any) -> Any:
    """
    Recursively replace any dict that has exactly one key 'value' with that value.
    So IDP-shaped {'state': {'value': 'NC'}} becomes {'state': 'NC'} and matches
    expected {'state': 'North Carolina'} on the same path; state normalization then applies.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_unwrap_value_dicts(item) for item in obj]
    if isinstance(obj, dict):
        if set(obj.keys()) == {"value"}:
            return _unwrap_value_dicts(obj["value"])
        # If actual is nested as {"fields": {"state": "NC", ...}}, use inner so paths match expected.
        if set(obj.keys()) == {"fields"} and isinstance(obj.get("fields"), dict):
            return _unwrap_value_dicts(obj["fields"])
        return {k: _unwrap_value_dicts(v) for k, v in obj.items()}
    return obj


def _flatten_value(val: Any) -> Any:
    """Return a comparable representation: primitives as-is, list/dict as JSON string."""
    if val is None or isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, list):
        return json.dumps(val, sort_keys=True)
    if isinstance(val, dict):
        return json.dumps(val, sort_keys=True)
    return str(val)


def flatten_for_compare(obj: Any, prefix: str = "") -> dict[str, Any]:
    """
    Flatten nested dict/list into path -> value pairs. Paths use dot notation (e.g. address.city).
    Lists are treated as a single comparable value (JSON string). So nested/weird structures
    become a set of field/value pairs for comparison.
    """
    if obj is None:
        return {prefix: None} if prefix else {}
    if isinstance(obj, (str, int, float, bool)):
        return {prefix: obj} if prefix else {}
    if isinstance(obj, list):
        return {prefix: _flatten_value(obj)} if prefix else {}
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            out.update(flatten_for_compare(v, path))
        return out
    return {prefix: str(obj)} if prefix else {}


# Document/run passes when field-level accuracy is at or above this percentage.
PASS_ACCURACY_THRESHOLD_PCT = 90.0


def field_accuracy_pct(score: int, total_fields: int) -> float | None:
    """Percentage of compared fields that match (0–100)."""
    if not total_fields:
        return None
    return round(100.0 * score / total_fields, 1)


def passes_accuracy_threshold(score: int, total_fields: int) -> bool:
    """True when score/total_fields meets PASS_ACCURACY_THRESHOLD_PCT (default 90%)."""
    pct = field_accuracy_pct(score, total_fields)
    return pct is not None and pct >= PASS_ACCURACY_THRESHOLD_PCT


def compare_fields(
    expected: dict[str, Any],
    actual: dict[str, Any],
    field_types: Optional[dict[str, str]] = None,
) -> tuple[bool, int, int, list[dict]]:
    """
    Compare expected vs actual with optional field type hints. Flattens nested JSON
    so that all field/value pairs (including nested and weird keys) are compared.
    Returns (pass, score, total_fields, diff_details).
    """
    expected = canonicalize_field_map(_unwrap_value_dicts(expected))
    # Normalize actual: use inner dict if it's wrapped as {"fields": {...}} so paths match expected
    if isinstance(actual, dict) and set(actual.keys()) == {"fields"} and isinstance(actual.get("fields"), dict):
        actual = actual["fields"]
    actual = canonicalize_field_map(_unwrap_value_dicts(actual) if isinstance(actual, dict) else {})
    flat_expected = flatten_for_compare(expected)
    flat_actual = flatten_for_compare(actual) if isinstance(actual, dict) else {}
    field_types = field_types or {}
    total = len(flat_expected)
    score = 0
    details = []
    for path, exp_val in flat_expected.items():
        canon = canonical_field_key(path)
        # Pull actual by canonical key first, then legacy dotted paths
        act_val = _get_value_by_path(actual, canon) if isinstance(actual, dict) else None
        if act_val is None and isinstance(actual, dict):
            act_val = actual.get(canon)
        if act_val is None:
            act_val = _get_value_by_path(actual, path) if isinstance(actual, dict) else None
        if act_val is None:
            act_val = flat_actual.get(canon) or flat_actual.get(path)
        # Type hint: try full path first, then leaf key (e.g. field_types["city"] for "address.city")
        hint = field_types.get(path) or field_types.get(path.split(".")[-1])
        norm_exp, type_exp = normalize_value(exp_val, hint)
        norm_act, type_act = normalize_value(act_val, hint)
        match = norm_exp == norm_act
        sentinel_match = (
            match
            and _is_blank_sentinel(exp_val if not isinstance(exp_val, dict) else str(exp_val))
            and (act_val is None or _is_blank_sentinel(act_val if not isinstance(act_val, dict) else str(act_val)))
        )
        if match:
            score += 1
        details.append({
            "field": canon or path,
            "expected_raw": exp_val,
            "actual_raw": act_val,
            "expected_normalized": norm_exp,
            "actual_normalized": norm_act,
            "type": type_exp,
            "match": match,
            "sentinel_match": sentinel_match,
        })
    pass_ = passes_accuracy_threshold(score, total) if total else False
    return (pass_, score, total, details)
