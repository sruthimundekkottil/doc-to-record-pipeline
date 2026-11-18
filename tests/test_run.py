"""Check status routing, stage events, and failed extraction records."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.models import ExtractedInvoice
from app.pipeline import run
from app.pipeline.extract import LlmUsage, TransientExtractionError


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """Use a valid extraction with mocked I/O for orchestration tests."""
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.touch()
    data = json.loads((Path(__file__).resolve().parents[1] / "data/ground_truth/inv_001.json").read_text())
    data["field_confidence"] = {"total": 0.99}
    invoice = ExtractedInvoice.model_validate(data)
    extract = MagicMock(return_value=(invoice, LlmUsage("test-model", 100, 50)))
    save = MagicMock(side_effect=lambda path, inv, **kwargs: {"status": kwargs["status"], "line_items": []})
    monkeypatch.setattr(run, "extract_text", MagicMock(return_value=("invoice text", "native")))
    monkeypatch.setattr(run, "extract_fields", extract)
    monkeypatch.setattr(run.db, "save_document", save)
    monkeypatch.setattr(run.db, "transaction", MagicMock())
    monkeypatch.setattr(run.db, "duplicate_exists", MagicMock(return_value=False))
    return pdf_path, invoice, extract, save


def test_process_routes_clean_invoice_and_captures_stage_events(pipeline):
    """Pass approved data and completed stage events into atomic persistence."""
    path, invoice, extract, save = pipeline
    record = run.process(path)
    assert record["status"] == "approved"
    assert record["failures"] == []
    extract.assert_called_once_with("invoice text")
    assert save.call_args.args == (path.resolve(), invoice)
    events = save.call_args.kwargs["events"]
    assert [event["stage"] for event in events] == ["ingest", "text", "extract", "validate", "route"]
    assert events[2]["prompt_tokens"] == 100
    assert events[2]["output_tokens"] == 50


def test_duplicate_routes_to_review(pipeline):
    """Retain the extracted document with a duplicate validation failure."""
    path, _, _, save = pipeline
    run.db.duplicate_exists.return_value = True
    record = run.process(path)
    assert record["status"] == "needs_review"
    assert [failure["code"] for failure in record["failures"]] == ["DUPLICATE"]
    assert save.call_args.kwargs["events"][3]["detail"]["failures"] == record["failures"]


def test_transient_error_retains_received_metadata_and_audit(pipeline):
    """Preserve a temporary error and store only received metadata and events."""
    path, _, extract, save = pipeline
    extract.side_effect = TransientExtractionError("HTTP 503")
    with pytest.raises(TransientExtractionError):
        run.process(path)
    assert save.call_args.args == (path.resolve(), None)
    assert save.call_args.kwargs["status"] == "received"
    assert save.call_args.kwargs["events"][2]["outcome"] == "transient_error"


def test_missing_input_is_audited_as_failed(pipeline):
    """Record an ingestion failure without invoking extraction."""
    path, _, extract, save = pipeline
    record = run.process(path.parent / "missing.pdf")
    assert record["status"] == "failed"
    extract.assert_not_called()
    assert save.call_args.args[1] is None
    assert save.call_args.kwargs["events"][0]["detail"]["error_type"] == "FileNotFoundError"
