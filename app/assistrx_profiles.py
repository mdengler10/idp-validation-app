"""AssistRx IDP test suite: reference documents (enrollment, insurance, copay)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Shared IDP runtime defaults (assistrx-idp-enrollment-project config-default.properties)
ASSISTRX_ORG_ID = "d59e8a52-cb48-4e53-b6c6-3db5f2224e1e"
ASSISTRX_REGION = "us-east-1"
ASSISTRX_PLATFORM = "production"

ENROLLMENT_ACTION_ID = "4561352f-1a5c-44de-9316-906d855f7631"
ENROLLMENT_ACTION_VERSION = "1.8.0"
INSURANCE_ACTION_ID = "2fafc453-6e9e-4627-b621-201d88cf4637"
INSURANCE_ACTION_VERSION = "1.2.0"
COPAY_ACTION_ID = "f65ff768-4d61-4b1f-9883-8d28a07181f5"
COPAY_ACTION_VERSION = "1.0.0"

# Master_Prompt keys used by EnrollmentPatientMap.dwl (IDP enrollment v1.6.0)
ENROLLMENT_FIELD_KEYS = [
    "patient_name",
    "date_of_birth",
    "gender",
    "primary_phone",
    "patient_email",
    "patient_email_address",
    "address",
    "city",
    "state",
    "zip",
    "primary_language",
    "uninsured",
    "diagnosis_code",
    "product_name",
    "dosage",
    "frequency",
    "route_of_administration",
    "duration_or_days_supply",
    "refills",
    "practice_name",
    "provider_name",
    "provider_npi",
    "provider_dea_number",
    "provider_phone",
    "provider_fax",
    "provider_address",
    "provider_city",
    "provider_state",
    "provider_zip",
    "provider_facility_type",
    "prescriber_state_license",
    "primary_insurance_name",
    "group_number",
    "primary_insurance_phone_number",
    "member_id",
    "member_name",
    "office_contact_name",
    "office_contact_email",
    "provider_signature_present",
    "signature_date",
    "secondary_or_prescription_insurance_company",
    "secondary_or_prescription_insurance_policy_id",
    "secondary_or_prescription_insurance_group",
    "secondary_or_prescription_insurance_rx_pcn",
    "secondary_or_prescription_insurance_rx_bin",
    "secondary_or_prescription_insurance_phone",
    "allergies",
    "concomitant_medicines",
    "previous_treatment",
    "ssn_last_four",
]

INSURANCE_FIELD_KEYS = [
    "Member_Name",
    "Member_ID",
    "Group_Number",
    "Insurance_Company",
    "Insurance_Plan_Name",
    "Effective_Date",
    "Patient_First_Name",
    "Patient_Last_Name",
    "Patient_DOB",
    "Patient_Phone",
    "Rx_BIN",
    "Rx_PCN",
    "Rx_Group",
    "Payer_ID",
    "Issuer_Number",
    "Dependents",
]

COPAY_FIELD_KEYS = [
    "patient_first_name",
    "patient_last_name",
    "patient_dob",
    "patient_phone",
    "person_completing_form",
    "practice_name",
    "provider_name",
    "check_payable_to",
    "patient_out_of_pocket_cost",
    "hcpcs_code",
    "date_of_service",
    "claim_reimbursement_amount",
    "product_selection",
    "product_name",
]

ENROLLMENT_FIELD_TYPES = {
    "date_of_birth": "date",
    "signature_date": "date",
    "primary_phone": "phone",
    "provider_phone": "phone",
    "primary_insurance_phone_number": "phone",
    "secondary_or_prescription_insurance_phone": "phone",
    "state": "state",
    "provider_state": "state",
    "refills": "string",
    "dosage": "string",
}

INSURANCE_FIELD_TYPES = {
    "Effective_Date": "date",
    "Patient_DOB": "date",
    "Patient_Phone": "phone",
}

COPAY_FIELD_TYPES = {
    "patient_dob": "date",
    "date_of_service": "date",
    "patient_phone": "phone",
    "patient_out_of_pocket_cost": "string",
    "claim_reimbursement_amount": "string",
}


@dataclass(frozen=True)
class DocumentProfile:
    id: str
    name: str
    category: str  # enrollment | insurance | copay
    brand: str  # vyvgart | theloryx | assistivan | somatuline | vivitrol
    document_id: str
    action_id: str
    action_version: str
    field_keys: list[str] = field(default_factory=list)
    field_types: dict[str, str] = field(default_factory=dict)
    org_id: str = ASSISTRX_ORG_ID
    region: str = ASSISTRX_REGION
    platform: str = ASSISTRX_PLATFORM

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "brand": self.brand,
            "document_id": self.document_id,
            "field_keys": self.field_keys,
            "field_types": self.field_types,
            "idp": {
                "platform": self.platform,
                "region": self.region,
                "org_id": self.org_id,
                "action_id": self.action_id,
                "action_version": self.action_version,
            },
        }


def _enrollment(brand: str, suffix: str, label: str) -> DocumentProfile:
    doc_id = f"enrollment-{brand}-{suffix}"
    return DocumentProfile(
        id=doc_id,
        name=f"{label} Enrollment",
        category="enrollment",
        brand=brand,
        document_id=doc_id,
        action_id=ENROLLMENT_ACTION_ID,
        action_version=ENROLLMENT_ACTION_VERSION,
        field_keys=list(ENROLLMENT_FIELD_KEYS),
        field_types=dict(ENROLLMENT_FIELD_TYPES),
    )


def _insurance(brand: str, suffix: str, label: str) -> DocumentProfile:
    doc_id = f"insurance-{brand}-{suffix}"
    return DocumentProfile(
        id=doc_id,
        name=f"{label} Insurance Card",
        category="insurance",
        brand=brand,
        document_id=doc_id,
        action_id=INSURANCE_ACTION_ID,
        action_version=INSURANCE_ACTION_VERSION,
        field_keys=list(INSURANCE_FIELD_KEYS),
        field_types=dict(INSURANCE_FIELD_TYPES),
    )


def _copay(brand: str, suffix: str, label: str) -> DocumentProfile:
    doc_id = f"copay-{brand}-{suffix}"
    return DocumentProfile(
        id=doc_id,
        name=f"{label} CoPay Claim",
        category="copay",
        brand=brand,
        document_id=doc_id,
        action_id=COPAY_ACTION_ID,
        action_version=COPAY_ACTION_VERSION,
        field_keys=list(COPAY_FIELD_KEYS),
        field_types=dict(COPAY_FIELD_TYPES),
    )


# Reference set: 5 enrollment + 3 insurance + 3 copay (insurance/copay brands unchanged)
PROFILES: list[DocumentProfile] = [
    _enrollment("vyvgart", "1", "Vyvgart"),
    _enrollment("theloryx", "1", "Theloryx"),
    _enrollment("assistivan", "1", "Assistivan"),
    _enrollment("somatuline", "1", "Somatuline (Ipsen CARES)"),
    _enrollment("vivitrol", "1", "Vivitrol"),
    _insurance("vyvgart", "1", "Vyvgart"),
    _insurance("theloryx", "1", "Theloryx"),
    _insurance("assistivan", "1", "Assistivan"),
    _copay("vyvgart", "1", "Vyvgart"),
    _copay("theloryx", "1", "Theloryx"),
    _copay("assistivan", "1", "Assistivan"),
]

PROFILES_BY_ID: dict[str, DocumentProfile] = {p.id: p for p in PROFILES}


def get_profile(profile_id: str) -> Optional[DocumentProfile]:
    return PROFILES_BY_ID.get(profile_id)


def canonical_suite_document_id(document_id: str) -> str:
    """
    Map ad-hoc saved-GT ids (e.g. PDF filename stems) to suite profile document_id
    so IDP target, extraction, and compare use enrollment field keys.
    """
    key = (document_id or "").strip()
    if not key or key.lower() == "default":
        return key
    for profile in PROFILES:
        if profile.document_id == key or profile.id == key:
            return profile.document_id
    low = key.lower()
    if "aletheon" in low or "theloryx" in low:
        return "enrollment-theloryx-1"
    if "somatuline" in low or "ipsen" in low or "lanreotide" in low:
        return "enrollment-somatuline-1"
    if "vivitrol" in low:
        return "enrollment-vivitrol-1"
    if "vyvgart" in low:
        return "enrollment-vyvgart-1"
    if "assistivan" in low:
        return "enrollment-assistivan-1"
    return key


def profile_for_suite_document_id(document_id: str) -> Optional[DocumentProfile]:
    """Resolve suite profile from document_id or canonical alias."""
    canon = canonical_suite_document_id(document_id)
    for profile in PROFILES:
        if profile.document_id == canon or profile.id == canon:
            return profile
    return None


def list_profiles() -> list[DocumentProfile]:
    return list(PROFILES)
