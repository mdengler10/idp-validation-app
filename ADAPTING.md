# Adapting this app beyond AssistRx

The **core product** is customer-agnostic: save ground truth → call MuleSoft IDP → fuzzy-compare fields → track scores.

Almost everything named **AssistRx / Vyvgart / Theloryx / Assistivan / enrollment–insurance–copay** is a **customer-specific suite layer** on top of that core. For a new customer, keep the core and replace the suite layer.

---

## Mental model: two layers

| Layer | Keep for every customer? | What it is |
|-------|--------------------------|------------|
| **Core** | Yes | FastAPI app, SQLite models, runs/results/overrides, fuzzy compare (`compare.py`, `normalize.py`), IDP HTTP client (`idp_client.py`), generic UI (`/runs`, `/extract-edit`, `/upload-form`, overrides, reset) |
| **Suite / customer pack** | Replace | Document catalog, org/action IDs, field key lists, extract rules for that customer's IDP payload shape, suite dashboard copy (`/suite`) |

You do **not** need to rewrite scoring, run history, or the IDP poll loop. You **do** need new profiles, field lists, action IDs, and usually a small extract tweak if their Document Actions return a different JSON shape.

---

## What is AssistRx-specific today

Treat these as the “customer pack” to swap out:

| File / area | AssistRx-specific content |
|-------------|---------------------------|
| `app/assistrx_profiles.py` | Org ID, region, enrollment/insurance/copay **action IDs + versions**, field key lists, brand names (Vyvgart…), `PROFILES` list |
| `app/assistrx_extract.py` | Assumes enrollment/copay use **`Master_Prompt` JSON string**; insurance uses **flat PascalCase fields** |
| `app/templates/suite.html` | Titles, section labels (“Patient enrollment forms”, etc.), hardcoded category keys `enrollment` / `insurance` / `copay` |
| `app/templates/suite_document.html` | Works generically off `profile`; mostly fine |
| `app/templates/index.html` | Nav/copy says “AssistRx Test Suite” |
| `app/routes.py` / `app/service.py` | Import suite helpers from `assistrx_*`; suite APIs under `/api/suite/...` (names are fine to keep) |
| Docs (`README.md`, `HANDOFF.md`) | AssistRx narrative |

**Usually leave alone:**

- `app/compare.py`, `app/normalize.py`, `app/idp_client.py`, `app/models.py`, `app/db.py`
- Run detail / override / reset flows
- Connected App credential storage (still needed; different customer = different Connected App + org)

---

## Checklist: stand up a new customer suite

Work top-to-bottom. You can do this in a branch named e.g. `customer/<name>-suite`.

### 1. Gather inputs from Anypoint / the customer

For each document type you will test:

- [ ] **Organization ID**
- [ ] **Region** (often `us-east-1`) and platform (production / staging / eu)
- [ ] **Document Action ID** + **version** per type
- [ ] **Sample PDFs** (at least one per form you care about)
- [ ] **Output field names** from a real IDP execution (Exchange / API response / Upload Form in this app)
- [ ] Note the **payload shape** (see below)

Connected App: client credentials with IDP scopes for that org (or a Connected App that can call those actions).

### 2. Decide payload extraction strategy

Run one PDF through **/upload-form** or **/suite** extract and inspect `fields`.

| Shape you see | What to do in extract code |
|---------------|----------------------------|
| Flat map: `fields.Member_Name.value`, `fields.Patient_DOB.value`, … | Like current **insurance**: read keys directly |
| Prompt blob: `fields.Master_Prompt.value` = JSON string of many keys | Like current **enrollment/copay**: parse JSON then pick keys |
| Nested sections: `fields.section_1.foo` | Often handled by `normalize_body_fields` in `compare.py`; may still need a thin unwrap in extract |
| Tables only / unusual layout | Extend extract; compare already flattens many nested structures |

Write down which shape each document type uses **before** building the profile list.

### 3. Replace the profile catalog

Edit `app/assistrx_profiles.py` (or rename — see optional cleanup below):

1. Change constants at the top:
   - Org ID, region, platform
   - One `*_ACTION_ID` / `*_ACTION_VERSION` per document type
2. Replace field key lists with the customer’s output names (from step 1).
3. Set `field_types` for fuzzy match (`date`, `phone`, `state`, `number`, `string`) where helpful.
4. Replace `PROFILES` with the new documents:

```python
# Example: fictional customer with two types
_invoice("acme", "1", "ACME Invoice A"),
_invoice("acme", "2", "ACME Invoice B"),
_po("acme", "1", "ACME Purchase Order"),
```

Each profile needs:

- Unique `id` / `document_id` (stable forever — used as GT key)
- Human `name` (shown on dashboard)
- `category` string (groups cards on `/suite`)
- `action_id` / `action_version` (and org/region if different from defaults)

Helpers like `_enrollment` / `_insurance` / `_copay` are just factories — rename or add `_invoice`, `_lab_report`, etc. to match the customer’s types.

### 4. Update extract logic

Edit `app/assistrx_extract.py` → `extract_from_idp_body`:

- Branch on `profile.category` (or a new `profile.extract_mode` if you prefer)
- For each category, produce a **flat** `dict[field_name → value]` used for compare
- Reuse `_master_prompt_object` only if they still use Master_Prompt; otherwise delete that branch for unused types

Also update `ground_truth_for_compare` if saved GT still wraps values as `{ "value": "..." }` (keep that unwrap — it is generic).

### 5. Update suite UI copy and categories

In `app/templates/suite.html`:

- Change page title / H1 / subtitle away from “AssistRx”
- Update `section_titles` / `section_desc` dicts to your category keys

Category keys in the template **must match** `profile.category` values in Python (e.g. `"invoice"`, `"po"`).

In `app/templates/index.html`, rename the suite nav link.

### 6. Fresh local data for the new customer

Do **not** reuse AssistRx ground truth in a shared DB for a different customer.

Options:

- Delete or rename local `data/idp_validation.db` and start clean, **or**
- Set `IDP_VALIDATION_DB=/path/to/customer_b.db` so suites stay isolated

Credentials entered in the UI are per-DB; new customer → new Connected App values.

### 7. Smoke test

1. Start app → `/suite` shows only the new cards  
2. Open one document → Action ID/version match Anypoint  
3. Extract → fields look right (edit if needed) → Save ground truth  
4. Run test → score + run detail with ✓/✗  
5. Confirm fuzzy cases (dates/phones) still behave as expected  

---

## Optional cleanup (nice for a reusable product)

Not required for the first new customer, but helpful if you’ll repeat this often:

| Step | Why |
|------|-----|
| Rename `assistrx_profiles.py` → `suite_profiles.py` (and `assistrx_extract.py` → `suite_extract.py`) | Removes customer branding from module names |
| Update imports in `routes.py` / `service.py` | Follow the rename |
| Move org/action IDs to env or `config/customer.yaml` | Swap customers without editing Python |
| Drive suite sections from `list_profiles()` categories instead of hardcoding in `suite.html` | New categories appear automatically |
| Keep AssistRx as one pack under `customers/assistrx/` | Multi-tenant style; bigger refactor |

A pragmatic first pass for a teammate: **edit the existing `assistrx_*` files in place** for customer B, rename modules later if the pattern sticks.

---

## What you can leave as “AssistRx demo” forever

If the goal is a **fork per customer** (simplest for demos):

1. Clone this repo  
2. Replace profiles + extract + suite labels  
3. Wipe `data/`  
4. Ship  

If the goal is **one codebase, many customers**, invest in the optional cleanup (config-driven profiles) before the third customer.

---

## Common pitfalls

1. **Wrong action ID/version** → extract succeeds but fields empty or wrong shape  
2. **Field keys don’t match IDP output names** (case-sensitive) → everything looks “missing” / 0%  
3. **Comparing Master_Prompt wrapper instead of inner JSON** → fix extract, not compare  
4. **Reusing old `document_id`s** after changing meaning → GT/scores attach to the wrong form; always new IDs for new forms  
5. **Python 3.14** → dependency build fails; use 3.12/3.13  
6. **Running uvicorn from a sandboxed environment** → DNS errors calling IDP; use a normal terminal  

---

## Minimal “hello customer B” example

Suppose customer B has one Document Action that returns flat fields `Invoice_Number`, `Vendor_Name`, `Invoice_Date`.

1. In profiles: one category `invoice`, field keys those three, `field_types={"Invoice_Date": "date"}`, their org/action/version.  
2. In extract: for `category == "invoice"`, read flat field values (copy the insurance branch).  
3. In `suite.html`: one section `"invoice": "Invoices"`.  
4. Fresh DB → save GT → run test.

That’s enough to prove the core still works without any AssistRx enrollment concepts.

---

## Related docs

- [HANDOFF.md](./HANDOFF.md) — AssistRx day-to-day use and architecture  
- [CONTRIBUTING.md](./CONTRIBUTING.md) — small PR habits  
- [REQUIREMENTS.md](./REQUIREMENTS.md) — REQ-1 / REQ-2 (keep these for every customer)  
