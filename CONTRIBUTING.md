# Contributing

**New customer / different document types?** Follow [ADAPTING.md](./ADAPTING.md) before renaming modules or wiping AssistRx profiles.

## Dev setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Use a normal system terminal for live IDP calls (DNS/network).

## Adding a suite document (most common change)

1. Open `app/assistrx_profiles.py`
2. Append a profile to `PROFILES` using `_enrollment`, `_insurance`, or `_copay`
3. Reload the app and open `/suite`
4. Set ground truth for the new card

Example:

```python
_enrollment("productx", "1", "Product X"),
```

Creates `document_id` = `enrollment-productx-1`.

## Changing field lists or fuzzy rules

- Field keys / type hints: `app/assistrx_profiles.py` (`ENROLLMENT_FIELD_KEYS`, etc.)
- Normalization: `app/normalize.py`
- IDP payload parsing: `app/assistrx_extract.py`
- Compare logic: `app/compare.py`

## UI

Jinja templates live under `app/templates/`. Suite dashboard: `suite.html`, per-document: `suite_document.html`. Styles: `app/static/style.css`.

## Pull requests

- Keep secrets out of commits (`data/`, env files, Connected App values)
- Prefer small PRs: one profile batch, or one feature
- Update `HANDOFF.md` if you change the suite workflow or architecture
