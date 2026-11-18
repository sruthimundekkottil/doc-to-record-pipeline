"""HTTP routes for invoice processing and review."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app import db
from app.pipeline import run
from app.pipeline.extract import TransientExtractionError

app = FastAPI(title="Document-to-record pipeline")


def _response(record):
    return JSONResponse(jsonable_encoder(record, custom_encoder={Decimal: str}))


@app.post("/documents")
def upload_document(file: UploadFile):
    """Process a multipart PDF and return its stored record."""
    try:
        return _response(run.process_upload(file.file, file.filename or "invoice.pdf", file.content_type))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except TransientExtractionError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/documents/{document_id}")
def get_document(document_id: UUID):
    """Fetch one document with line items and validation failures."""
    record = db.get_document(document_id)
    if record is None:
        raise HTTPException(404, "Document not found.")
    return _response(record)


@app.get("/documents")
def list_documents(
    status: Literal["received", "approved", "needs_review", "failed"] | None = None,
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
):
    """List document summaries with status filtering and pagination."""
    return _response(db.list_documents(status, limit, offset))


@app.post("/documents/{document_id}/approve")
def approve_document(document_id: UUID):
    """Record a human approval of a flagged document."""
    try:
        record = db.approve_document(document_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if record is None:
        raise HTTPException(404, "Document not found.")
    return _response(record)


@app.get("/health")
def health():
    """Check database connectivity and model-key configuration."""
    checks = db.health()
    return JSONResponse(checks, status_code=200 if all(checks.values()) else 503)
