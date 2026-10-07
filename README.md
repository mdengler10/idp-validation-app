# IDP Validation App

Validate MuleSoft **Intelligent Document Processing (IDP)** extraction against human **ground truth**, with fuzzy field matching, run history, and an AssistRx-focused test suite.

Built for the AssistRx enrollment POC: compare what IDP pulls from enrollment forms, insurance cards, and CoPay claims against known-correct values, and track success rates over time.

## For teammates

| Doc | Read when… |
|-----|------------|
| **[HANDOFF.md](./HANDOFF.md)** | Running the AssistRx suite, architecture, day-to-day workflow |
| **[ADAPTING.md](./ADAPTING.md)** | Reusing this app for **another customer** (new docs / doc types) — keep core, replace suite pack |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | Small PRs, adding documents within a suite |
| [REQUIREMENTS.md](./REQUIREMENTS.md) | Original REQ-1 / REQ-2 product requirements |

## Requirements

- **REQ-1 Fuzzy matching:** Same content, different formatting → match (dates, phones, states, blank sentinels like `NOT FOUND`)
- **REQ-2 Manual override:** Per-field mark correct ↔ incorrect; reporting uses override when set

## Stack

- Python 3.12 or 3.13 (not 3.14)
- FastAPI + Uvicorn
- SQLAlchemy + SQLite (`data/` — gitignored)
- Jinja2 UI + httpx for IDP API calls

## Run

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash restart-app.sh
```

Open **http://127.0.0.1:8000/suite**

Or:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## AssistRx test suite

Dashboard: **/suite**

| Category | Profiles |
|----------|----------|
| Enrollment | Vyvgart, Theloryx, Assistivan, Somatuline (Ipsen CARES), Vivitrol |
| Insurance | Vyvgart, Theloryx, Assistivan |
| CoPay | Vyvgart, Theloryx, Assistivan |

Per document: **Extract → edit → Save ground truth → Run test**. Scores appear on the suite dashboard and under **/runs**.

Profiles and IDP action IDs: `app/assistrx_profiles.py`.

## Credentials

Enter Anypoint Connected App **Client ID / Client Secret** in the suite UI (stored only in local SQLite), or set:

```bash
export IDP_CLIENT_ID="..."
export IDP_CLIENT_SECRET="..."
```

Never commit secrets or the `data/` folder.

## Data persistence

Runtime data lives in **`data/idp_validation.db`** (gitignored): ground truth, runs, overrides, cached IDP credentials.

Override path: `IDP_VALIDATION_DB=/path/to/idp_validation.db`.

## Other UI routes

| Path | Use |
|------|-----|
| `/suite` | Primary suite dashboard |
| `/runs` | Run history + all-time field accuracy |
| `/extract-edit` | Generic extract → edit → save GT |
| `/upload-form` | Raw IDP Document Action upload |
| `/saved-ground-truth` | Manage saved GT documents |

## License / sharing

Private handoff repo for internal iteration. Treat IDP credentials and any shared DB copies as confidential.
