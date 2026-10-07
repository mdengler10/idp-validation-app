"""REQ-1: Fuzzy normalizers for date, number, string before comparison."""
import re
from typing import Any, Optional

from dateutil import parser as date_parser

# US state abbreviation <-> full name for loose matching (e.g. NC <-> North Carolina)
_US_STATES = {
    "al": "alabama", "alabama": "alabama", "ak": "alaska", "alaska": "alaska",
    "az": "arizona", "arizona": "arizona", "ar": "arkansas", "arkansas": "arkansas",
    "ca": "california", "california": "california", "co": "colorado", "colorado": "colorado",
    "ct": "connecticut", "connecticut": "connecticut", "de": "delaware", "delaware": "delaware",
    "fl": "florida", "florida": "florida", "ga": "georgia", "georgia": "georgia",
    "hi": "hawaii", "hawaii": "hawaii", "id": "idaho", "idaho": "idaho",
    "il": "illinois", "illinois": "illinois", "in": "indiana", "indiana": "indiana",
    "ia": "iowa", "iowa": "iowa", "ks": "kansas", "kansas": "kansas",
    "ky": "kentucky", "kentucky": "kentucky", "la": "louisiana", "louisiana": "louisiana",
    "me": "maine", "maine": "maine", "md": "maryland", "maryland": "maryland",
    "ma": "massachusetts", "massachusetts": "massachusetts", "mi": "michigan", "michigan": "michigan",
    "mn": "minnesota", "minnesota": "minnesota", "ms": "mississippi", "mississippi": "mississippi",
    "mo": "missouri", "missouri": "missouri", "mt": "montana", "montana": "montana",
    "ne": "nebraska", "nebraska": "nebraska", "nv": "nevada", "nevada": "nevada",
    "nh": "new hampshire", "new hampshire": "new hampshire", "nj": "new jersey", "new jersey": "new jersey",
    "nm": "new mexico", "new mexico": "new mexico", "ny": "new york", "new york": "new york",
    "nc": "north carolina", "north carolina": "north carolina", "nd": "north dakota", "north dakota": "north dakota",
    "oh": "ohio", "ohio": "ohio", "ok": "oklahoma", "oklahoma": "oklahoma",
    "or": "oregon", "oregon": "oregon", "pa": "pennsylvania", "pennsylvania": "pennsylvania",
    "ri": "rhode island", "rhode island": "rhode island", "sc": "south carolina", "south carolina": "south carolina",
    "sd": "south dakota", "south dakota": "south dakota", "tn": "tennessee", "tennessee": "tennessee",
    "tx": "texas", "texas": "texas", "ut": "utah", "utah": "utah",
    "vt": "vermont", "vermont": "vermont", "va": "virginia", "virginia": "virginia",
    "wa": "washington", "washington": "washington", "wv": "west virginia", "west virginia": "west virginia",
    "wi": "wisconsin", "wisconsin": "wisconsin", "wy": "wyoming", "wyoming": "wyoming",
    "dc": "district of columbia", "district of columbia": "district of columbia",
}


def normalize_date(value: Any) -> Optional[str]:
    """Parse common date formats and return ISO 8601 date string, or None if not parseable.
    Uses dayfirst=True so 08-11-2023 and 2023/11/08 both parse to the same date (8 Nov 2023).
    """
    if value is None or _is_blank_sentinel(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    s = str(value).strip()
    # Member IDs / long numeric strings are not dates — avoid dateutil overflow on insurance cards.
    digits_only = re.sub(r"\D", "", s)
    if len(digits_only) >= 12 and not re.search(r"[/\-.\s]", s):
        return None
    try:
        dt = date_parser.parse(s, dayfirst=True)
        return dt.date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def normalize_number(value: Any) -> Optional[str]:
    """Strip thousand separators, normalize decimal; return canonical string or None."""
    if value is None or _is_blank_sentinel(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    s = str(value).strip()
    # Remove common thousand separators
    s = s.replace(",", "").replace(" ", "")
    # Normalize decimal
    if "٫" in s or "，" in s:
        s = s.replace("٫", ".").replace("，", ".")
    try:
        f = float(s)
        return str(f)
    except ValueError:
        return None


def _is_blank_sentinel(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return True
    u = s.upper()
    return u in ("NOT FOUND", "NONE", "N/A", "NA", "NULL")


def normalize_string(value: Any, case_sensitive: bool = False) -> str:
    """Trim; optionally lowercase for comparison. NOT FOUND / N/A → empty."""
    if value is None or _is_blank_sentinel(value):
        return ""
    s = str(value).strip()
    return s if case_sensitive else s.lower()


def normalize_state(value: Any) -> Optional[str]:
    """Normalize US state to canonical full name (lowercase) so 'NC' and 'North Carolina' match."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    s = str(value).strip().lower()
    return _US_STATES.get(s)


def normalize_phone(value: Any) -> Optional[str]:
    """Strip all non-digits; return digits only so 555-123-4567 and 555 123 4567 match."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    s = str(value).strip()
    digits = re.sub(r"\D", "", s)
    return digits if digits else None


def _looks_like_phone(s: str) -> bool:
    """True if string has 7+ digits and only digits/separators (space, dash, dot, parens)."""
    if not s or len(s) > 25:
        return False
    digits = sum(1 for c in s if c.isdigit())
    allowed = set("0123456789 -().+")
    return digits >= 7 and all(c in allowed for c in s)


def normalize_value(value: Any, hint: Optional[str] = None) -> tuple[Optional[str], str]:
    """
    Normalize using hint if provided, else try date, number, phone-like, then string.
    Returns (normalized_value, effective_type) for storage in diff_details.
    """
    if hint == "date":
        n = normalize_date(value)
        return (n, "date") if n is not None else (normalize_string(value), "string")
    if hint == "number":
        n = normalize_number(value)
        return (n, "number") if n is not None else (normalize_string(value), "string")
    if hint == "string":
        return (normalize_string(value), "string")
    if hint == "phone":
        n = normalize_phone(value)
        return (n, "phone") if n is not None else (normalize_string(value), "string")
    if hint == "state":
        n = normalize_state(value)
        return (n, "state") if n is not None else (normalize_string(value), "string")
    # Fallback: try date, then number, then phone-like string, then state-like, then string
    n = normalize_date(value)
    if n is not None:
        return (n, "date")
    n = normalize_number(value)
    if n is not None:
        return (n, "number")
    s = str(value).strip() if value is not None else ""
    if s and _looks_like_phone(s):
        p = normalize_phone(value)
        if p is not None:
            return (p, "phone")
    # Try state: 2-letter or known state name/abbreviation
    st = normalize_state(value)
    if st is not None:
        return (st, "state")
    return (normalize_string(value), "string")
