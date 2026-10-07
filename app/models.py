"""SQLite models: runs, results, overrides, expected documents."""
from datetime import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, Boolean
from sqlalchemy.orm import relationship

from app.db import Base


class Run(Base):
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    label = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    results = relationship("Result", back_populates="run", cascade="all, delete-orphan")


class Result(Base):
    __tablename__ = "results"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False)
    document_id = Column(String(255), nullable=False)
    pass_ = Column("pass", Boolean, nullable=False)
    score = Column(Integer, nullable=True)  # e.g. 8 for 8/10 fields match
    total_fields = Column(Integer, nullable=True)
    diff_details = Column(Text, nullable=True)  # JSON: per-field expected, actual, normalized, match
    created_at = Column(DateTime, default=datetime.utcnow)
    run = relationship("Run", back_populates="results")
    overrides = relationship("ResultOverride", back_populates="result", cascade="all, delete-orphan")


class ResultOverride(Base):
    """Per-field override: user marks auto correct→incorrect or auto incorrect→correct."""
    __tablename__ = "result_overrides"
    id = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column(Integer, ForeignKey("results.id"), nullable=False)
    field_name = Column(String(255), nullable=False)
    override_pass = Column(Boolean, nullable=False)  # True = user says correct, False = user says incorrect
    result = relationship("Result", back_populates="overrides")


class ExpectedDocument(Base):
    """Ground truth: one JSON object of field→value per document."""
    __tablename__ = "expected_documents"
    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(String(255), unique=True, nullable=False)
    fields_json = Column(Text, nullable=False)  # JSON object
    field_types_json = Column(Text, nullable=True)  # optional: {"invoiceDate": "date", "amount": "number"}
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ResetBackup(Base):
    """Single row storing snapshot of runs/results/overrides for Undo after Reset."""
    __tablename__ = "reset_backup"
    id = Column(Integer, primary_key=True, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    data_json = Column(Text, nullable=False)  # JSON: {runs: [...], results: [...], overrides: [...]}


class SavedGroundTruth(Base):
    """Saved expected (ground truth) from Extract & Edit: document name + fields JSON for loading into New Run."""
    __tablename__ = "saved_ground_truth"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(512), nullable=False)  # user-facing "Document Name"
    document_id = Column(String(512), unique=True, nullable=False)  # key when loading into expected (e.g. slug of name)
    fields_json = Column(Text, nullable=False)  # IDP-shaped fields/tables for comparison
    created_at = Column(DateTime, default=datetime.utcnow)


class IdpCredentials(Base):
    """Single row: stored IDP credentials so they persist across refreshes and browser sessions."""
    __tablename__ = "idp_credentials"
    id = Column(Integer, primary_key=True, default=1)
    credentials_json = Column(Text, nullable=True)  # JSON: platform, region, org_id, action_id, action_version, client_id, client_secret
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
