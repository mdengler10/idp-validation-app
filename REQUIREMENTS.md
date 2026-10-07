# Requirements to get started

These are the requirements we are building to first.

## REQ-1: Fuzzy matching (format-agnostic comparison)

- Values that represent the same content but differ only in formatting must be treated as a **match**.
- **Dates:** Normalize to canonical form (e.g. ISO 8601). Examples that must count as equal: `03/13/2025`, `2025-03-13`, `March 13, 2025`.
- **Numbers:** Optional normalization (strip thousand separators, normalize decimal).
- **Strings:** Trim whitespace; optional case-insensitive comparison.
- Support field-level type hints (date / number / string) so the right normalizer is applied. Store raw and normalized values in diff details for auditing.

## REQ-2: Manual override (correct the app’s verdict)

- User can **mark as incorrect:** override a result the app marked as correct → treat as incorrect in reporting.
- User can **mark as correct:** override a result the app marked as incorrect → treat as correct in reporting.
- Scope: **per field** (override individual field verdict).
- Storage for overrides; reporting uses override when set. UI: “Mark correct” / “Mark incorrect” and “Clear override.”

## Supporting scope

- Document key matching (document ID or filename).
- Load expected JSON per document (bulk or single).
- Ingest IDP results via upload or API (POST from Mule).
- Store runs, results, and history; reset all metrics (runs + results + overrides; expected data kept).
