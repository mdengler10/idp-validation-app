# Handoff guide — IDP Validation App

This document is for teammates continuing work on the AssistRx / MuleSoft IDP accuracy testing app.

**Reusing this app for a different customer (new docs / doc types)?** Start with **[ADAPTING.md](./ADAPTING.md)** — what to keep, what to strip, and a file-by-file checklist.

## What this app is

A small **FastAPI + SQLite** web app that:

1. Saves **ground truth** (correct field values) per reference document
2. Calls **MuleSoft IDP** Document Actions with a PDF/image
3. Compares IDP output to ground truth (fuzzy matching for dates, phones, states)
4. Tracks **field-level success %** per document and across runs

It is **not** the Mule enrollment intake app (`assistrx-idp-enrollment-project`). It is a **validation harness** for the same IDP document actions that app uses.

## Quick start

```bash
cd idp-validation-app
python3.13 -m venv .venv   # 3.12 or 3.13 — not 3.14 (pydantic/PyO3)
source .venv/bin/activate
pip install -r requirements.txt
bash restart-app.sh
# or: uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open:

| URL | Purpose |
|-----|---------|
| http://127.0.0.1:8000/suite | **Primary UI** — AssistRx test suite dashboard |
| http://127.0.0.1:8000/suite/{profile_id} | One document: extract, save GT, run test |
| http://127.0.0.1:8000/runs | All comparison runs + all-time stats |
| http://127.0.0.1:8000 | Home / other tools |

**Important:** Run uvicorn from a normal terminal (not a sandboxed Cursor shell) so IDP DNS/network works. Use `us-east-1` for production IDP Runtime.

## Credentials (do not commit)

IDP Connected App credentials are entered in the UI and stored in the local SQLite DB (`data/idp_validation.db`), which is **gitignored**.

You can also set env vars:

```bash
export IDP_CLIENT_ID="..."
export IDP_CLIENT_SECRET="..."
```

Get Client ID/Secret from Anypoint → Access Management → Connected Apps (client credentials grant, IDP scopes). Org ID / Action IDs are already defaulted in profiles for the AssistRx org.

## Current suite profiles

Defined in `app/assistrx_profiles.py` → `PROFILES`.

| Category | Profiles | IDP action (defaults) |
|----------|----------|------------------------|
| Enrollment | Vyvgart, Theloryx, Assistivan, Somatuline (Ipsen CARES), Vivitrol | `4561352f-…` v1.6.0 |
| Insurance | Vyvgart, Theloryx, Assistivan | `2fafc453-…` v1.1.0 |
| CoPay | Vyvgart, Theloryx, Assistivan | `f65ff768-…` v1.0.0 |

Each profile has a stable `document_id` (e.g. `enrollment-vyvgart-1`). Ground truth and scores key off that ID.

## Day-to-day workflow (per document)

1. Open `/suite` → click a document card
2. Paste Connected App **Client ID / Secret** once (persists locally)
3. Upload the PDF
4. **Extract from IDP** → correct fields → **Save ground truth** (once)
5. Later: upload PDF again → **Run test** → see % correct + link to run detail
6. On run detail: optional per-field **Mark correct / Mark incorrect** overrides

Dashboard shows last score per document and a summary strip (GT count, tested count, average %).

## How to add more documents

### Same type (another enrollment / insurance / copay PDF)

Edit `app/assistrx_profiles.py` and append to `PROFILES`:

```python
_enrollment("vyvgart", "2", "Vyvgart Form B"),
_insurance("theloryx", "2", "Theloryx Card B"),
_copay("assistivan", "2", "Assistivan CoPay B"),
```

Rules:

- Keep `suffix` / brand unique so `document_id` stays unique
- Restart uvicorn (or rely on `--reload`)
- Set ground truth for the new card on `/suite`

### New category (e.g. consent form)

1. Add field key list + type hints in `assistrx_profiles.py`
2. Add helper `_consent(...)` like `_enrollment`
3. Add extract logic in `app/assistrx_extract.py` if the IDP payload shape differs
4. Add a section in `app/templates/suite.html` if you want a new dashboard group

### Different IDP action for one document only

Pass custom `action_id` / `action_version` on that `DocumentProfile` (see `DocumentProfile` dataclass).

## Architecture map

| Path | Role |
|------|------|
| `app/main.py` | FastAPI entry, middleware, DB init |
| `app/routes.py` | HTTP API + Jinja page routes (`/suite`, `/api/...`) |
| `app/service.py` | Runs, compare orchestration, suite overview/summary, GT save |
| `app/assistrx_profiles.py` | Suite document catalog + IDP defaults |
| `app/assistrx_extract.py` | Flatten IDP body (`Master_Prompt` JSON string → fields) |
| `app/compare.py` | Field compare + nested/section flattening |
| `app/normalize.py` | Fuzzy date / phone / state / blank sentinels (`NOT FOUND`, etc.) |
| `app/idp_client.py` | OAuth token + POST execution + poll |
| `app/models.py` / `app/db.py` | SQLAlchemy models + SQLite |
| `app/templates/suite*.html` | Suite UI |
| `data/` | Local DB + logs (**not in git**) |
| `REQUIREMENTS.md` | Original REQ-1 / REQ-2 product requirements |

### Comparison shape

- **Enrollment / CoPay:** IDP returns `fields.Master_Prompt.value` as a JSON string → extract to flat keys (`patient_name`, …)
- **Insurance:** Flat IDP fields (`Member_Name`, `Patient_DOB`, …)
- Ground truth saved from the UI may use `{ "field": { "value": "..." } }` wrappers; `ground_truth_for_compare` unwraps them

### Scoring

- Score = matching fields / fields present in **expected (ground truth)** after cleaning empties
- Fuzzy match: dates, phones (digits only), US states (abbr ↔ name), trim/case, blank sentinels
- Overrides change effective pass/fail for reporting without rewriting GT

## Related Mule project (context only)

The production-style intake app lives separately:

`AnypointStudio/studio-workspace/assistrx-idp-enrollment-project`

That project splits packets, classifies pages, calls the same IDP actions, and writes Salesforce. This validation app tests **IDP extraction accuracy** for reference PDFs, not the full Mule pipeline.

Field mapping reference in Mule: `EnrollmentPatientMap.dwl`, `InsuranceCopayMap.dwl`.

## Adapting beyond AssistRx

See **[ADAPTING.md](./ADAPTING.md)** for the full guide. Short version:

- **Keep:** compare, normalize, IDP client, runs/overrides, SQLite, generic extract-edit UI  
- **Replace:** `assistrx_profiles.py` (org, actions, field keys, `PROFILES`), extract branches in `assistrx_extract.py`, suite page titles/categories in `suite.html` / `index.html`  
- **Fresh DB** per customer (`data/` or `IDP_VALIDATION_DB=...`) so AssistRx ground truth is not mixed in  

## Suggested next iterations

Priority ideas for a teammate:

1. **Add more suite PDFs** via `PROFILES` as forms become available (same customer)
2. **Port the suite to another customer** using [ADAPTING.md](./ADAPTING.md)
3. **Batch “run all with GT”** button on `/suite` (upload folder or re-run stored files)
4. **Export CSV/JSON** of last scores for stakeholder reports
5. **Optional Mule webhook** — POST packet `enrollmentExtraction` into `/api/suite/...` without re-uploading PDF
6. **Tighten field lists per form** — some enrollment forms don’t fill every Master_Prompt key; per-profile `field_keys` subsets reduce noise
7. **Tests** — unit tests for `assistrx_extract.extract_from_idp_body` and `compare.compare_fields` with fixture JSON
8. **Config-driven profiles** (YAML/env) so swapping customers does not require editing Python module names

## Ops notes

- **Python:** 3.12 or 3.13 only (3.14 breaks pydantic-core build)
- **Port:** 8000 (`restart-app.sh` kills whatever is on 8000 first)
- **DB path override:** `IDP_VALIDATION_DB=/path/to/file.db`
- **Backup:** copy `data/idp_validation.db` if you need to share GT/history offline (contains local credentials if saved in UI — treat as secret)
- **Do not commit:** `data/`, `.venv/`, `.env*`, Connected App secrets

## Contact / ownership

Handed off from local development under `/Users/mdengler/idp-validation-app`. Continue iterating in this repo; open PRs against `main`.
