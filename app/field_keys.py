"""Canonical field key normalization for IDP compare/extract."""
from __future__ import annotations

from typing import Any


_WRAPPER_PREFIXES = (
    "fields.Master_Prompt.",
    "Master_Prompt.",
    "fields.master_prompt.",
    "master_prompt.",
    "value.",  # IDP nested Master_Prompt.value.{field} saves from Extract UI
)

_IDP_FIELD_WRAPPER_KEYS = frozenset({
    "value",
    "confidencescore",
    "confidence_score",
    "geometry",
    "text",
    "answer",
    "displayvalue",
})

_IDP_META_PATH_SEGMENTS = frozenset({
    "confidencescore",
    "confidence_score",
    "geometry",
    "text",
    "answer",
    "displayvalue",
})


def is_idp_field_wrapper(obj: Any) -> bool:
    """True for IDP leaf nodes like {value, confidenceScore?, geometry?}."""
    if not isinstance(obj, dict) or "value" not in obj:
        return False
    return all(str(k).lower() in _IDP_FIELD_WRAPPER_KEYS for k in obj)


def is_idp_meta_field_key(key: str) -> bool:
    """True when a flattened path is IDP metadata (e.g. Group_Number.confidenceScore)."""
    parts = (key or "").strip().split(".")
    return any(p.lower() in _IDP_META_PATH_SEGMENTS for p in parts[1:])


def canonical_field_key(key: str) -> str:
    """
    Normalize IDP / UI field paths to compare keys (e.g. patient_name).
    Handles fields saved as fields.Master_Prompt.patient_name or Master_Prompt.value.patient_name.
    """
    k = (key or "").strip()
    if is_idp_meta_field_key(k):
        return ""
    changed = True
    while changed:
        changed = False
        for prefix in _WRAPPER_PREFIXES:
            if k.startswith(prefix):
                k = k[len(prefix):]
                changed = True
                break
    if k.startswith("fields."):
        k = k[len("fields."):]
    if "." in k:
        parts = k.split(".")
        if parts[-1].lower() == "value" and len(parts) > 1:
            k = ".".join(parts[:-1])
        head = k.split(".", 1)[0].lower()
        if head in (
            "fields",
            "master_prompt",
            "section_1",
            "section_2",
            "section_3",
            "section_4",
            "section_5",
            "section_6",
        ):
            k = k.split(".")[-1]
    return k


def canonicalize_field_map(obj: Any) -> dict[str, Any]:
    """Rewrite dict keys to canonical compare keys; last value wins on collision."""
    if not isinstance(obj, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in obj.items():
        ck = canonical_field_key(str(k))
        if ck:
            out[ck] = v
    return out
