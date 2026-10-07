"""Extract flat compare fields from AssistRx IDP execution bodies."""
from __future__ import annotations

import json
from typing import Any, Optional

from app.assistrx_profiles import DocumentProfile
from app.compare import flatten_section_shaped_to_fields, normalize_body_fields
from app.field_keys import canonical_field_key, canonicalize_field_map, is_idp_field_wrapper, is_idp_meta_field_key


def _parse_json_string(value: Any) -> Any:
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
    return value


_ENROLLMENT_BLOB_MARKERS = frozenset(
    {"patient_name", "date_of_birth", "address", "provider_name", "member_id", "primary_phone"}
)


def _looks_like_enrollment_schema(obj: dict[str, Any]) -> bool:
    if not isinstance(obj, dict) or len(obj) < 2:
        return False
    keys = {str(k).lower() for k in obj}
    return len(keys & _ENROLLMENT_BLOB_MARKERS) >= 2


def _try_parse_enrollment_json(value: Any) -> Optional[dict[str, Any]]:
    """
    Parse enrollment custom-schema JSON from a string or IDP field node.
    Tolerates a trailing extra '}' that sometimes appears in IDP Master_Prompt output.
    """
    if isinstance(value, dict):
        if _looks_like_enrollment_schema(value):
            return value
        if set(value.keys()) <= {"value", "text", "answer"}:
            return _try_parse_enrollment_json(_unwrap_idp_value(value))
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s.startswith("{"):
        return None
    candidates = [s]
    if s.endswith("}}"):
        candidates.append(s[:-1])
    if s.endswith("}"):
        candidates.append(s.rstrip("}"))
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and _looks_like_enrollment_schema(parsed):
            return parsed
    return None


def _deep_parse_json_strings(obj: Any, depth: int = 0) -> Any:
    """
    Recursively expand JSON-looking strings (matches run_new.html unwrapJsonStrings).
    IDP often nests the full custom-schema output inside string values.
    """
    if depth > 16:
        return obj
    if isinstance(obj, str):
        parsed = _parse_json_string(obj)
        if parsed is not obj and isinstance(parsed, dict):
            return _deep_parse_json_strings(parsed, depth + 1)
        return obj
    if isinstance(obj, dict):
        return {k: _deep_parse_json_strings(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_parse_json_strings(item, depth + 1) for item in obj]
    return obj


def _largest_schema_dict_from_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Find the biggest dict embedded in a fields map (Master_Prompt or any JSON blob field)."""
    best: dict[str, Any] = {}
    mp = _master_prompt_object(fields)
    if len(mp) > len(best):
        best = mp
    for _key, raw_val in fields.items():
        unwrapped = _unwrap_idp_value(raw_val)
        if isinstance(unwrapped, str) and unwrapped.strip().startswith("{"):
            parsed = _parse_json_string(unwrapped)
            if isinstance(parsed, dict) and len(parsed) > len(best):
                best = parsed
        elif isinstance(unwrapped, dict) and len(unwrapped) > len(best):
            if not str(_key).lower().startswith("section_"):
                best = unwrapped
    return best


_MAX_IDP_UNWRAP_DEPTH = 48


def _unwrap_idp_value(val: Any) -> Any:
    """Unwrap IDP field nodes: {value}, {text}, {answer: {value}} (bounded — IDP can nest value wrappers deeply)."""
    current = val
    for _ in range(_MAX_IDP_UNWRAP_DEPTH):
        if current is None or not isinstance(current, dict):
            return current
        if "answer" in current and isinstance(current.get("answer"), dict):
            nxt = current["answer"].get("value")
            if nxt is current:
                break
            current = nxt
            continue
        if "value" in current and len(current) <= 3:
            nxt = current["value"]
            if nxt is current:
                break
            current = nxt
            continue
        if "text" in current and len(current) <= 2:
            return current["text"]
        break
    return current


def _idp_field_value(fields: dict[str, Any], key: str) -> Any:
    node = fields.get(key)
    if node is None:
        for sk in fields:
            if sk.lower() == key.lower():
                node = fields[sk]
                break
    if node is None:
        return None
    return _unwrap_idp_value(node)


def _flat_master_prompt_from_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """When normalize_body_fields flattened Master_Prompt JSON into dotted keys."""
    prefix = "Master_Prompt."
    alt = "fields.Master_Prompt."
    out: dict[str, Any] = {}
    for key, val in fields.items():
        leaf = None
        if key.startswith(prefix):
            leaf = key[len(prefix):]
        elif key.startswith(alt):
            leaf = key[len(alt):]
        if not leaf:
            continue
        out[leaf] = _unwrap_idp_value(val)
    return out


def _master_prompt_object(fields: dict[str, Any]) -> dict[str, Any]:
    for blob_key in ("Master_Prompt", "master_prompt", "value", "Value"):
        node = fields.get(blob_key)
        if node is None:
            for sk in fields:
                if sk.lower() == blob_key.lower():
                    node = fields[sk]
                    break
        if node is None:
            continue
        raw = _unwrap_idp_value(node)
        parsed = _try_parse_enrollment_json(raw)
        if isinstance(parsed, dict) and parsed:
            return parsed
        raw = _parse_json_string(raw)
        if isinstance(raw, dict):
            return raw
    flat = _flat_master_prompt_from_fields(fields)
    if flat:
        return flat
    return {}


def _is_idp_sentinel_string(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    return val.strip().upper() in ("NOT FOUND", "NONE", "N/A", "NA", "NULL")


def _merge_field_map(
    base: dict[str, Any],
    extra: dict[str, Any],
    profile_keys: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Merge extra into base without overwriting a real value with null/empty.
    Includes IDP sentinel strings (None, Not Found) so compare reflects model output.
    """
    key_lookup = {k.lower(): k for k in (profile_keys or [])}
    out = dict(base)
    for raw_key, raw_val in extra.items():
        leaf = _leaf_key(str(raw_key))
        canon = key_lookup.get(leaf.lower(), leaf)
        if profile_keys and leaf.lower() not in key_lookup:
            continue
        val = _unwrap_idp_value(raw_val)
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        existing = out.get(canon)
        if existing is not None and _scalar_has_value(existing):
            if not _scalar_has_value(val) and not _is_idp_sentinel_string(val):
                continue
        if canon not in out or not _scalar_has_value(existing):
            out[canon] = val
    return out


# Prompt field names that differ from suite compare keys / ground truth
_ENROLLMENT_FIELD_ALIASES: dict[str, str] = {
    "primary_diagnosis": "diagnosis_code",
    "patient_email": "patient_email_address",
}


def _apply_field_aliases(
    fields: dict[str, Any],
    profile_keys: Optional[list[str]] = None,
    body: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Copy values from alias keys when canonical compare key is empty."""
    if not profile_keys:
        return fields
    key_set = {k.lower() for k in profile_keys}
    out = dict(fields)
    prompt_extra = _prompts_object(body) if body else {}
    for src, dest in _ENROLLMENT_FIELD_ALIASES.items():
        if dest.lower() not in key_set:
            continue
        dest_key = next((k for k in profile_keys if k.lower() == dest.lower()), dest)
        if _scalar_has_value(out.get(dest_key)) or _is_idp_sentinel_string(out.get(dest_key)):
            continue
        src_val = (
            out.get(src)
            or prompt_extra.get(src)
            or prompt_extra.get(src.lower())
        )
        if src_val is not None:
            out[dest_key] = _unwrap_idp_value(src_val)
    return out


def _prompts_dict_from_node(prompts: Any) -> dict[str, Any]:
    if prompts is None:
        return {}
    if isinstance(prompts, str):
        prompts = _parse_json_string(prompts)
    if isinstance(prompts, list):
        return _fields_from_field_array(prompts)
    if not isinstance(prompts, dict):
        return {}
    out: dict[str, Any] = {}
    for key, node in prompts.items():
        if str(key).lower() in ("master_prompt", "classification"):
            mp = _unwrap_idp_value(node)
            mp = _parse_json_string(mp) if isinstance(mp, str) else mp
            if isinstance(mp, dict):
                out = _merge_field_map(out, mp)
            continue
        val = _unwrap_idp_value(node)
        if val is not None:
            out[str(key)] = val
    return out


def _prompts_object(body: dict[str, Any]) -> dict[str, Any]:
    """Per-prompt IDP shape: {prompts: {field_name: {answer: {value: ...}}}}."""
    if not isinstance(body, dict):
        return {}
    merged: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [body]
    seen = 0
    while stack and seen < 30:
        node = stack.pop()
        seen += 1
        if not isinstance(node, dict):
            continue
        if "prompts" in node:
            merged = _merge_field_map(merged, _prompts_dict_from_node(node.get("prompts")))
        for key in ("result", "execution", "output", "data", "fields"):
            child = node.get(key)
            if isinstance(child, dict):
                stack.append(child)
    return merged


def _leaf_key(key: str) -> str:
    """section_1.patient_name -> patient_name; Master_Prompt.patient_name -> patient_name."""
    return canonical_field_key(key)


def _collect_schema_fields(
    fields: dict[str, Any],
    profile_keys: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Merge all enrollment/copay field sources from an IDP fields map:
    - Master_Prompt JSON blob
    - Flat custom-schema keys (patient_name, etc.)
    - Dotted / section-prefixed keys after normalize_body_fields
    - Any top-level field whose value is a JSON object string
    """
    out: dict[str, Any] = {}

    mp = _largest_schema_dict_from_fields(fields)
    if mp:
        for pk, pv in mp.items():
            canon = canonical_field_key(str(pk))
            if not canon:
                continue
            if profile_keys:
                key_lookup_mp = {k.lower(): k for k in profile_keys}
                if canon.lower() not in key_lookup_mp:
                    continue
                canon = key_lookup_mp[canon.lower()]
            val = _unwrap_idp_value(pv)
            if canon not in out or out.get(canon) in (None, ""):
                out[canon] = val

    key_lookup = {k.lower(): k for k in (profile_keys or [])}

    for raw_key, raw_val in fields.items():
        if raw_key.lower() in ("master_prompt",):
            continue

        unwrapped = _unwrap_idp_value(raw_val)

        # JSON blob field (custom schema single output or misnamed Master_Prompt)
        if isinstance(unwrapped, str) and unwrapped.strip().startswith("{"):
            parsed = _parse_json_string(unwrapped)
            if isinstance(parsed, dict):
                for pk, pv in parsed.items():
                    if pk not in out or out.get(pk) in (None, ""):
                        out[pk] = pv
                continue

        # Nested section object not fully flattened
        if isinstance(unwrapped, dict) and "value" not in unwrapped and "answer" not in unwrapped:
            for sk, sv in unwrapped.items():
                suv = _unwrap_idp_value(sv)
                if suv is None:
                    continue
                canon = key_lookup.get(sk.lower(), sk)
                if profile_keys and sk.lower() not in key_lookup and canon == sk:
                    continue
                if canon not in out or out.get(canon) in (None, ""):
                    out[canon] = suv
            continue

        leaf = _leaf_key(raw_key)
        canon = key_lookup.get(leaf.lower(), leaf)
        if profile_keys and leaf.lower() not in key_lookup:
            continue
        if unwrapped is None:
            continue
        if canon not in out or out.get(canon) in (None, ""):
            out[canon] = unwrapped

    return out


def _fields_from_field_array(arr: Any) -> dict[str, Any]:
    """IDP shapes: fieldResults / extractionResults as [{name, value}, ...]."""
    if not isinstance(arr, list):
        return {}
    out: dict[str, Any] = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("fieldName") or item.get("key") or item.get("id")
        if not name:
            continue
        val = item.get("value")
        if val is None and isinstance(item.get("values"), list) and item["values"]:
            val = item["values"][0]
        if val is None:
            val = item
        out[str(name)] = val
    return out


def _expand_fields_container(fields: Any) -> dict[str, Any]:
    """Normalize fields node: dict, JSON string, or array of field objects."""
    if fields is None:
        return {}
    if isinstance(fields, str):
        fields = _parse_json_string(fields)
    if isinstance(fields, list):
        return _fields_from_field_array(fields)
    if not isinstance(fields, dict):
        return {}
    out = dict(fields)
    for array_key in ("fieldResults", "extractionResults", "results", "fields"):
        nested = out.get(array_key)
        if isinstance(nested, list) and nested:
            out.update(_fields_from_field_array(nested))
    return out


def _fields_map_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """Locate the IDP fields dict across common execution response shapes."""
    if not isinstance(body, dict):
        return {}

    candidates: list[dict[str, Any]] = []

    def add(node: Any) -> None:
        expanded = _expand_fields_container(node)
        if expanded:
            candidates.append(expanded)

    add(body.get("fields"))
    for path in ("result", "execution", "output", "data"):
        node = body.get(path)
        if isinstance(node, dict):
            add(node.get("fields"))
            if isinstance(node.get("execution"), dict):
                add(node["execution"].get("fields"))
            if isinstance(node.get("result"), dict):
                add(node["result"].get("fields"))

    docs = body.get("documents")
    if isinstance(docs, list) and docs:
        first = docs[0]
        if isinstance(first, dict):
            add(first.get("fields"))

    pages = body.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, dict):
                add(page.get("fields"))

    results = body.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            add(first.get("fields"))

    merged: dict[str, Any] = {}
    for c in candidates:
        merged.update(c)
    return merged


def _unwrap_ground_truth_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Saved ground truth may use IDP {value: x} wrappers per key (possibly nested deeply)."""
    out: dict[str, Any] = {}
    for key, val in fields.items():
        if isinstance(val, dict):
            out[key] = _unwrap_idp_value(val)
        else:
            out[key] = val
    return out


def _flatten_compare_map(fields: dict[str, Any]) -> dict[str, Any]:
    """Reduce a compare map to canonical keys with scalar (or sentinel string) leaves."""
    out: dict[str, Any] = {}
    for raw_key, raw_val in fields.items():
        if is_idp_meta_field_key(str(raw_key)):
            continue
        canon = canonical_field_key(str(raw_key))
        if not canon:
            continue
        leaf = _unwrap_idp_value(raw_val)
        if isinstance(leaf, str) and leaf.strip().startswith("{"):
            parsed = _parse_json_string(leaf)
            if isinstance(parsed, dict):
                for pk, pv in parsed.items():
                    c = canonical_field_key(str(pk))
                    if c:
                        out[c] = _unwrap_idp_value(pv)
                continue
        if isinstance(leaf, dict):
            if set(leaf.keys()) == {"value"}:
                leaf = _unwrap_idp_value(leaf)
            elif leaf:
                for pk, pv in leaf.items():
                    c = canonical_field_key(str(pk))
                    if c:
                        out[c] = _unwrap_idp_value(pv)
                continue
        out[canon] = leaf
    return out


def _pick_keys(source: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    """Build compare dict for listed keys; case-insensitive lookup."""
    out: dict[str, Any] = {}
    for key in keys:
        val = source.get(key)
        if val is None:
            for sk, sv in source.items():
                if sk.lower() == key.lower():
                    val = sv
                    break
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        out[key] = val
    return out


def _scalar_has_value(val: Any) -> bool:
    """True when a leaf compare value is present (not null/blank/sentinel-only)."""
    if val is None:
        return False
    if isinstance(val, dict):
        leaf = _unwrap_idp_value(val)
        if leaf is not val:
            return _scalar_has_value(leaf)
        if not leaf:
            return False
        return any(_scalar_has_value(v) for v in leaf.values())
    if isinstance(val, list):
        return any(_scalar_has_value(v) for v in val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return False
        u = s.upper()
        if u in ("NOT FOUND", "NONE", "N/A", "NA", "NULL"):
            return False
        return True
    return True


def field_node_has_value(node: Any) -> bool:
    """True when an IDP field node carries a non-empty value (incl. Master_Prompt JSON)."""
    if node is None:
        return False
    if isinstance(node, dict):
        raw = node.get("value", node.get("text", node.get("displayValue")))
        if isinstance(raw, str) and raw.strip().startswith("{"):
            parsed = _parse_json_string(raw)
            if isinstance(parsed, dict):
                return any(_scalar_has_value(v) for v in parsed.values())
        unwrapped = _unwrap_idp_value(node)
        return _scalar_has_value(unwrapped)
    if isinstance(node, str):
        if node.strip().startswith("{"):
            parsed = _parse_json_string(node)
            if isinstance(parsed, dict):
                return any(_scalar_has_value(v) for v in parsed.values())
        return _scalar_has_value(node)
    return _scalar_has_value(node)


def _fields_map_has_values(fields: Any) -> bool:
    if not isinstance(fields, dict) or not fields:
        return False
    for key, node in fields.items():
        k = str(key).lower()
        if k in ("master_prompt", "classification") and field_node_has_value(node):
            return True
        if field_node_has_value(node):
            return True
    return False


def idp_body_has_meaningful_fields(body: dict[str, Any]) -> bool:
    """
    True when an IDP execution JSON includes at least one extracted value.
    Stub poll bodies (status + empty/null fields) return False — matches AssistRx Mule refetch logic.
    """
    if not body or not isinstance(body, dict):
        return False

    maps = _fields_map_from_body(body)
    if _fields_map_has_values(maps):
        return True

    prompts = body.get("prompts")
    if isinstance(prompts, dict):
        for node in prompts.values():
            if field_node_has_value(node):
                return True
    elif isinstance(prompts, list):
        merged = _fields_from_field_array(prompts)
        if _fields_map_has_values(merged):
            return True

    for top_key in ("extractionResult", "extractionResults", "fieldResults"):
        node = body.get(top_key)
        if isinstance(node, list):
            merged = _fields_from_field_array(node)
            if _fields_map_has_values(merged):
                return True

    pages = body.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, dict) and _fields_map_has_values(page.get("fields")):
                return True

    for path in ("result", "execution", "output", "data"):
        node = body.get(path)
        if isinstance(node, dict) and idp_body_has_meaningful_fields(node):
            return True

    return False


def has_meaningful_field_values(fields: dict[str, Any]) -> bool:
    """True when a flat compare map has at least one non-blank value."""
    if not isinstance(fields, dict):
        return False
    return any(_scalar_has_value(v) for v in fields.values())


def has_profile_field_values(
    fields: dict[str, Any],
    profile: Optional[DocumentProfile] = None,
) -> bool:
    """
    True when at least one profile compare key has a real (non-sentinel) value.
    Prevents false positives from Classification-only IDP stubs.
    """
    if not isinstance(fields, dict) or not fields:
        return False
    keys = profile.field_keys if profile and profile.field_keys else None
    if not keys:
        return has_meaningful_field_values(fields)
    allowed = {k.lower() for k in keys}
    for raw_k, raw_v in fields.items():
        canon = canonical_field_key(str(raw_k))
        if canon.lower() not in allowed:
            continue
        if _scalar_has_value(_unwrap_idp_value(raw_v)):
            return True
    return False


def idp_body_extraction_incomplete(body: dict[str, Any]) -> bool:
    """
    True when IDP returned SUCCEEDED but only classification/metadata — not enrollment data.
    Matches AssistRx Mule refetch when extraction fields are not ready yet.
    """
    if not body or not isinstance(body, dict):
        return True
    if not idp_body_has_meaningful_fields(body):
        return True

    maps = _fields_map_from_body(body)
    mp = _master_prompt_object(maps)
    if mp and any(_scalar_has_value(v) for v in mp.values()):
        return False

    prompts = _prompts_object(body)
    for key, val in prompts.items():
        if str(key).lower() in ("classification", "master_prompt"):
            continue
        if _scalar_has_value(_unwrap_idp_value(val)):
            return False

    collected = _collect_schema_fields(maps, None)
    for key, val in collected.items():
        if str(key).lower() in ("classification", "master_prompt"):
            continue
        if _scalar_has_value(val):
            return False

    for top_key in ("extractionResult", "extractionResults", "fieldResults"):
        arr = body.get(top_key)
        if isinstance(arr, list):
            merged = _fields_from_field_array(arr)
            for key, val in merged.items():
                if str(key).lower() in ("classification",):
                    continue
                if _scalar_has_value(_unwrap_idp_value(val)):
                    return False

    return True


def _should_put_gt_value(val: Any) -> bool:
    """Same rules as suite UI getFieldsFromBody — keep None / Not Found strings."""
    if val is None:
        return False
    if isinstance(val, str) and not val.strip():
        return False
    return True


def _gt_put(out: dict[str, Any], key: str, val: Any) -> None:
    """Write into {canonical_key: {value: x}} like the suite Extract UI."""
    canon = canonical_field_key(str(key))
    if not canon:
        return
    u = _unwrap_idp_value(val)
    if isinstance(u, str) and u.strip().startswith("{"):
        parsed = _try_parse_enrollment_json(u) or _parse_json_string(u)
        if isinstance(parsed, dict):
            for pk, pv in parsed.items():
                _gt_put(out, pk, pv)
            return
    if isinstance(u, dict) and u:
        for pk, pv in u.items():
            _gt_put(out, pk, pv)
        return
    if not _should_put_gt_value(u) and not _is_idp_sentinel_string(u):
        return
    existing = out.get(canon)
    if existing is not None:
        ev = _unwrap_idp_value(existing)
        if _scalar_has_value(ev) and not _scalar_has_value(u) and not _is_idp_sentinel_string(u):
            return
    out[canon] = {"value": u}


def _ingest_fields_map_gt(out: dict[str, Any], fields: Any) -> None:
    """Port of suite getFieldsFromBody ingestFieldsMap + run_new fieldResults arrays."""
    if fields is None:
        return
    if isinstance(fields, str):
        fields = _parse_json_string(fields)
    if isinstance(fields, list):
        for item in fields:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("fieldName") or item.get("key") or item.get("id")
            if name is not None:
                _gt_put(out, str(name), item.get("value", item))
        return
    if not isinstance(fields, dict):
        return

    f = dict(fields)
    for array_key in ("fieldResults", "extractionResults", "results", "fields"):
        nested = f.get(array_key)
        if isinstance(nested, list) and nested:
            _ingest_fields_map_gt(out, nested)
            break

    for k, v in f.items():
        if str(k) in ("fieldResults", "extractionResults", "results", "fields"):
            continue
        if v is not None and isinstance(v, dict) and "value" in v:
            inner = v.get("value")
            if isinstance(inner, str) and inner.strip().startswith("{"):
                parsed = _parse_json_string(inner)
                if isinstance(parsed, dict):
                    for pk, pv in parsed.items():
                        _gt_put(out, pk, pv)
                    continue
            _gt_put(out, k, inner)
        else:
            _gt_put(out, k, v)

    if f and all(
        isinstance(f.get(k), dict)
        and not is_idp_field_wrapper(f.get(k))
        and not (len(f[k]) == 1 and "value" in f[k])
        for k in f
        if str(k) not in ("fieldResults", "extractionResults", "results", "fields")
    ):
        flat = flatten_section_shaped_to_fields(f)
        for k, wrapped in flat.items():
            _gt_put(out, k, wrapped)


def _ingest_prompts_gt(out: dict[str, Any], prompts: Any) -> None:
    if prompts is None:
        return
    if isinstance(prompts, str):
        prompts = _parse_json_string(prompts)
    if isinstance(prompts, list):
        _ingest_fields_map_gt(out, prompts)
        return
    if not isinstance(prompts, dict):
        return
    for k, v in prompts.items():
        if str(k).lower() == "master_prompt":
            mp = _unwrap_idp_value(v)
            if isinstance(mp, str) and mp.strip().startswith("{"):
                mp = _parse_json_string(mp)
            if isinstance(mp, dict):
                for pk, pv in mp.items():
                    _gt_put(out, pk, pv)
            continue
        _gt_put(out, k, v)


def _walk_idp_body_gt(out: dict[str, Any], node: Any, depth: int = 0) -> None:
    if depth > 12 or not isinstance(node, dict):
        return
    _ingest_fields_map_gt(out, node.get("fields"))
    _ingest_prompts_gt(out, node.get("prompts"))
    tables = node.get("tables")
    if isinstance(tables, dict):
        _ingest_fields_map_gt(out, tables)
    elif isinstance(tables, list):
        for table in tables:
            if isinstance(table, dict):
                _ingest_fields_map_gt(out, table.get("fields"))
                _ingest_fields_map_gt(out, table)
    for array_key in ("extractionResult", "extractionResults", "fieldResults"):
        arr = node.get(array_key)
        if isinstance(arr, list):
            _ingest_fields_map_gt(out, arr)
    pages = node.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, dict):
                _ingest_fields_map_gt(out, page.get("fields"))
    for child_key in ("result", "execution", "output", "data", "documents", "results"):
        child = node.get(child_key)
        if isinstance(child, dict):
            _walk_idp_body_gt(out, child, depth + 1)
        elif isinstance(child, list) and child and isinstance(child[0], dict):
            _walk_idp_body_gt(out, child[0], depth + 1)


def _harvest_profile_fields_into(
    out: dict[str, Any],
    node: Any,
    profile_keys: list[str],
    depth: int = 0,
) -> None:
    """Walk entire IDP body; capture any key that matches a profile compare field (case-insensitive)."""
    if depth > 24 or node is None:
        return
    key_lookup = {k.lower(): k for k in profile_keys}
    if isinstance(node, dict):
        for raw_key, raw_val in node.items():
            rk = str(raw_key)
            if rk.lower() in (
                "status", "id", "executionid", "execution", "documentname", "documentid",
                "result", "extractionresult", "extractionresults", "documents", "results", "pages",
            ):
                _harvest_profile_fields_into(out, raw_val, profile_keys, depth + 1)
                continue
            canon = canonical_field_key(rk.replace(" ", "_"))
            if canon.lower() in key_lookup:
                dest = key_lookup[canon.lower()]
                leaf = _unwrap_idp_value(raw_val)
                if isinstance(leaf, dict):
                    for sk, sv in leaf.items():
                        sub = canonical_field_key(str(sk))
                        if sub.lower() in key_lookup:
                            d = key_lookup[sub.lower()]
                            svu = _unwrap_idp_value(sv)
                            if _should_put_gt_value(svu) or _is_idp_sentinel_string(svu):
                                out[d] = {"value": svu}
                    _harvest_profile_fields_into(out, leaf, profile_keys, depth + 1)
                    continue
                if _should_put_gt_value(leaf) or _is_idp_sentinel_string(leaf):
                    existing = out.get(dest)
                    if existing is None or not _scalar_has_value(_unwrap_idp_value(existing)):
                        out[dest] = {"value": leaf}
            _harvest_profile_fields_into(out, raw_val, profile_keys, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _harvest_profile_fields_into(out, item, profile_keys, depth + 1)


def idp_body_to_gt_field_shape(body: dict[str, Any]) -> dict[str, Any]:
    """
    Build the same {field: {value}} map the suite UI produces via getFieldsFromBody.
    This is the shape save_profile_ground_truth / ground_truth_for_compare expect.
    """
    if not body or not isinstance(body, dict):
        return {}
    body = _deep_parse_json_strings(body)
    out: dict[str, Any] = {}
    _walk_idp_body_gt(out, body, 0)
    meta = {
        "status", "id", "executionId", "execution", "documentName", "documentId",
        "result", "tables", "extractionResult", "extractionResults", "prompts",
        "fields", "documents", "results", "pages",
    }
    if not out:
        for k, v in body.items():
            if k in meta:
                continue
            if isinstance(v, dict):
                _gt_put(out, k, v)
    return out


def idp_body_to_gt_field_shape_for_profile(
    body: dict[str, Any],
    profile: DocumentProfile,
) -> dict[str, Any]:
    """GT-shaped map with profile-key harvest pass (used by extract / compare)."""
    if not body or not isinstance(body, dict):
        return {}
    parsed = _deep_parse_json_strings(body)
    out = idp_body_to_gt_field_shape(parsed)
    if profile.field_keys:
        _harvest_profile_fields_into(out, parsed, profile.field_keys, 0)
    return out


def _expand_blob_gt_fields(
    gt: dict[str, Any],
    profile_keys: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Expand lone Master_Prompt / value enrollment JSON blobs into per-field {value} entries."""
    if not isinstance(gt, dict) or not gt:
        return {}
    blob_keys = [k for k in gt if str(k).lower() in ("master_prompt", "value")]
    if not blob_keys:
        return gt
    key_lookup = {k.lower(): k for k in (profile_keys or [])}
    expanded: dict[str, Any] = {
        k: v for k, v in gt.items() if str(k).lower() not in ("master_prompt", "value")
    }
    for bk in blob_keys:
        parsed = _try_parse_enrollment_json(gt[bk])
        if not isinstance(parsed, dict):
            continue
        for pk, pv in parsed.items():
            canon = canonical_field_key(str(pk))
            if profile_keys and canon.lower() not in key_lookup:
                continue
            if profile_keys:
                canon = key_lookup[canon.lower()]
            expanded[canon] = {"value": _unwrap_idp_value(pv)}
    if len(expanded) > len(gt) - len(blob_keys):
        return expanded
    return gt


def gt_fields_for_extract_ui(body: dict[str, Any], profile: DocumentProfile) -> dict[str, Any]:
    """Per-field {value} map for suite Extract UI — same shape as Vyvgart/Theloryx enrollment docs."""
    shaped = idp_body_to_gt_field_shape_for_profile(body, profile)
    return _expand_blob_gt_fields(shaped, profile.field_keys)


def resolve_actual_for_compare(
    profile: DocumentProfile,
    idp_body: Optional[dict[str, Any]] = None,
    ui_fields: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Build actual compare map: server parse of IDP body, merged with optional UI-shaped fields.
    UI fields (from getFieldsFromBody / gatherFields) win when they carry a value.
    """
    from_body: dict[str, Any] = {}
    if isinstance(idp_body, dict) and idp_body:
        from_body = extract_from_idp_body(profile, idp_body)

    if not ui_fields or not isinstance(ui_fields, dict):
        return from_body

    from_ui = ground_truth_for_compare(ui_fields, profile)
    if profile.field_keys:
        from_ui = _pick_keys(from_ui, profile.field_keys) or from_ui

    if not from_body:
        return from_ui
    if not from_ui:
        return from_body
    # UI values first; fill gaps from server IDP parse
    return _merge_field_map(from_ui, from_body, profile.field_keys)


def _extract_direct_collect(profile: DocumentProfile, body: dict[str, Any]) -> dict[str, Any]:
    """Fallback: collect from fields map + per-prompt answers (legacy extract path)."""
    maps = _fields_map_from_body(body)
    out = _collect_schema_fields(maps, profile.field_keys)
    prompts = _prompts_object(body)
    if prompts:
        out = _merge_field_map(out, prompts, profile.field_keys)
    out = _apply_field_aliases(out, profile.field_keys, body)
    if profile.field_keys:
        picked = _pick_keys(out, profile.field_keys)
        if picked:
            return picked
    return out


def extract_from_idp_body(profile: DocumentProfile, body: dict[str, Any]) -> dict[str, Any]:
    """
    Extract compare-ready fields using the SAME path as saved ground truth:
    IDP body -> UI-shaped {field: {value}} -> ground_truth_for_compare,
    merged with direct fields/prompts collection so no source is dropped.
    """
    if not body or not isinstance(body, dict):
        return {}
    parsed_body = _deep_parse_json_strings(body)
    shaped = idp_body_to_gt_field_shape_for_profile(parsed_body, profile)
    flat = ground_truth_for_compare(shaped, profile)
    direct = _extract_direct_collect(profile, parsed_body)
    flat = _merge_field_map(flat, direct, profile.field_keys)
    if profile.category in ("enrollment", "copay"):
        flat = _apply_field_aliases(flat, profile.field_keys, body)
    if profile.field_keys:
        flat = _pick_keys(flat, profile.field_keys) or flat
    return _flatten_compare_map(flat)


def ground_truth_for_compare(saved_fields: dict[str, Any], profile: DocumentProfile) -> dict[str, Any]:
    """
    Convert saved ground truth (IDP-shaped from Extract & Edit) to compare map.
    """
    flat = _unwrap_ground_truth_fields(saved_fields)
    if profile.category in ("enrollment", "copay"):
        if "Master_Prompt" in flat:
            mp = _try_parse_enrollment_json(flat["Master_Prompt"]) or _parse_json_string(flat["Master_Prompt"])
            if isinstance(mp, dict) and mp:
                flat = mp
        elif "master_prompt" in flat:
            mp = _try_parse_enrollment_json(flat["master_prompt"]) or _parse_json_string(flat["master_prompt"])
            if isinstance(mp, dict) and mp:
                flat = mp
        elif any(str(k).lower() == "value" for k in flat):
            for vk in flat:
                if str(vk).lower() != "value":
                    continue
                mp = _try_parse_enrollment_json(flat[vk])
                if isinstance(mp, dict) and mp:
                    flat = mp
                    break
        else:
            # Saved from UI as flat {field: {value}} — already unwrapped above
            collected = _collect_schema_fields(flat, profile.field_keys)
            if collected:
                flat = collected
    cleaned: dict[str, Any] = {}
    for k, v in flat.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        canon = canonical_field_key(k)
        if canon not in cleaned or cleaned.get(canon) in (None, ""):
            cleaned[canon] = v
    if profile.field_keys:
        cleaned = _pick_keys(cleaned, profile.field_keys) or cleaned
    return _flatten_compare_map(cleaned)


def is_idp_execution_or_wrapper(fields: dict[str, Any]) -> bool:
    """True when payload is a raw IDP body or {fields, tables} wrapper from the UI."""
    if not isinstance(fields, dict) or not fields:
        return False
    meta = {
        "status", "id", "executionId", "execution", "documentName", "documentId",
        "result", "tables", "extractionResult", "extractionResults", "prompts",
        "fields", "documents", "results", "pages",
    }
    if fields.keys() & (meta - {"fields", "tables", "prompts"}):
        return True
    if isinstance(fields.get("fields"), dict) and set(fields.keys()) <= {
        "fields", "tables", "prompts", "status", "id", "documentName", "documentId",
    }:
        return True
    return idp_body_has_meaningful_fields(fields)


def profile_for_document_id(document_id: str) -> Optional[DocumentProfile]:
    """Suite profile for document_id, including filename-based saved-GT aliases."""
    from app.assistrx_profiles import profile_for_suite_document_id

    return profile_for_suite_document_id(document_id)


# IDP metadata keys that sometimes appear in saved Extract UI payloads — not enrollment fields.
_COMPARE_META_KEYS = frozenset(
    {"confidencescore", "confidence_score", "geometry", "classification", "documentname", "status"}
)


def normalize_expected_for_profile(
    fields: dict[str, Any],
    profile: DocumentProfile,
) -> dict[str, Any]:
    """
    Canonicalize saved ground truth (often Master_Prompt.value.{field}) to enrollment compare keys.
    """
    if not isinstance(fields, dict) or not fields:
        return {}
    if is_idp_execution_or_wrapper(fields):
        return ground_truth_for_compare(fields, profile)
    shaped = ground_truth_for_compare(fields, profile)
    if shaped and profile.field_keys:
        picked = _pick_keys(shaped, profile.field_keys)
        if picked:
            shaped = picked
    if shaped:
        return _flatten_compare_map(shaped)
    flat = _flatten_compare_map(canonicalize_field_map(_unwrap_ground_truth_fields(fields)))
    if profile.field_keys:
        flat = _pick_keys(flat, profile.field_keys) or flat
    return {
        k: v
        for k, v in flat.items()
        if canonical_field_key(k).lower() not in _COMPARE_META_KEYS
    }


def _is_flat_compare_map(fields: dict[str, Any]) -> bool:
    """True when payload is already server-extracted scalars, not a raw IDP body or UI {value} map."""
    if not fields or is_idp_execution_or_wrapper(fields):
        return False
    for v in fields.values():
        if isinstance(v, dict) and v:
            if "value" in v or len(v) > 1 or any(isinstance(x, dict) for x in v.values()):
                return False
    return True


def prepare_actual_for_compare(
    fields: dict[str, Any],
    profile: Optional[DocumentProfile] = None,
) -> dict[str, Any]:
    """
    Normalize actual/compare payload: full IDP bodies, {value: x} maps, or flat strings.
    UI-shaped {field: {value}} uses the same path as ground truth.
    """
    if not isinstance(fields, dict) or not profile:
        return {}
    meta = {
        "status", "id", "executionId", "execution", "documentName", "documentId",
        "result", "tables", "extractionResult", "extractionResults", "prompts",
        "fields", "documents", "results", "pages",
    }
    looks_ui_shaped = any(
        isinstance(v, dict) and "value" in v for v in fields.values()
    )
    if looks_ui_shaped and not (fields.keys() & meta):
        return ground_truth_for_compare(fields, profile)
    if is_idp_execution_or_wrapper(fields):
        return extract_from_idp_body(profile, fields)
    flat = _flatten_compare_map(_unwrap_ground_truth_fields(fields))
    if profile.field_keys:
        picked = _pick_keys(flat, profile.field_keys)
        if picked:
            return picked
    return flat
