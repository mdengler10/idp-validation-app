"""Pydantic schemas for API."""
from typing import Any, Optional

from pydantic import BaseModel, Field


class ExpectedDocumentIn(BaseModel):
    document_id: str
    fields: dict[str, Any]
    field_types: Optional[dict[str, str]] = None  # e.g. {"invoiceDate": "date", "amount": "number"}


class ExpectedBulkIn(BaseModel):
    """Bulk load: object keyed by document_id."""
    documents: dict[str, dict[str, Any]]
    field_types_global: Optional[dict[str, str]] = None  # optional field name -> type for all docs


class RunFromJsonIn(BaseModel):
    """Create a run from two JSON bodies: expected (correct) and IDP result, keyed by document_id."""
    expected: dict[str, dict[str, Any]]
    idp: dict[str, dict[str, Any]]
    label: Optional[str] = None
    # Optional full IDP execution JSON per document_id (server-side extract when UI parse is empty)
    idp_bodies: Optional[dict[str, dict[str, Any]]] = None


class RunPatchIn(BaseModel):
    """Update run metadata from the Runs screen."""
    label: str = ""  # empty string clears the label (stored as NULL)


class IDPResultIn(BaseModel):
    document_id: str
    fields: dict[str, Any]


class OverrideIn(BaseModel):
    field_name: str
    override_pass: bool  # True = mark as correct, False = mark as incorrect


class RunOut(BaseModel):
    id: int
    label: Optional[str]
    created_at: str

    class Config:
        from_attributes = True


class FieldDiffOut(BaseModel):
    field: str
    expected_raw: Any
    actual_raw: Any
    expected_normalized: Any
    actual_normalized: Any
    type: str
    match: bool
    override_pass: Optional[bool] = None


class ResultOut(BaseModel):
    id: int
    run_id: int
    document_id: str
    pass_: bool = Field(alias="pass")
    score: Optional[int]
    total_fields: Optional[int]
    diff_details: Optional[list[dict]]
    overrides: Optional[dict[str, bool]] = None  # field_name -> override_pass
    effective_pass: Optional[bool] = None  # after applying overrides

    class Config:
        from_attributes = True
        populate_by_name = True
