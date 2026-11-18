"""Exercise real PostgreSQL commits and rollback in isolated test schemas."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import db
from app.models import ExtractedInvoice
from app.pipeline import extract, run

ROOT = Path(__file__).resolve().parents[1]


def test_existing_metadata_is_migrated(database, invoice):
    """Backfill old absolute paths and the weakest audited field confidence."""
    path = ROOT / "data/invoices/inv_006.pdf"
    events = [{"stage": "extract", "outcome": "success", "duration_ms": 1,
               "detail": {"field_confidence": {"vendor_name": 0.82, "total": 0.99}}}]
    record = db.save_document(path, invoice, events=events)
    with database.connection() as connection:
        connection.execute("UPDATE documents SET stored_path = %s, confidence = NULL WHERE id = %s", (str(path), record["id"]))
    db.migrate_document_metadata()
    migrated = db.get_document(record["id"])
    assert migrated["stored_path"] == "data/invoices/inv_006.pdf"
    assert migrated["confidence"] == Decimal("0.820")


def test_approval_is_atomic_and_preserves_validation_history(database, invoice):
    """Approve a flagged record and retain its failure details and override audit."""
    events = [{"stage": "validate", "outcome": "needs_review", "duration_ms": 1,
               "detail": {"failures": [{"code": "DUPLICATE"}]}}]
    record = db.save_document(ROOT / "data/invoices/inv_006.pdf", invoice, status="needs_review", events=events)
    approved = db.approve_document(record["id"])
    assert approved["status"] == "approved"
    assert approved["failures"] == [{"code": "DUPLICATE"}]
    assert len(approved["line_items"]) == 2
    with database.connection() as connection:
        log = connection.execute("SELECT detail FROM processing_log WHERE stage = 'approve'").fetchone()
    assert log["detail"]["action"] == "human_override"
    with pytest.raises(ValueError):
        db.approve_document(record["id"])
    assert db.get_document(uuid4()) is None
    assert db.approve_document(uuid4()) is None


@pytest.fixture
def database(monkeypatch):
    """Use a disposable schema without touching the application's tables."""
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests.")
    schema = "test_step4_" + uuid4().hex
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        connection.execute((ROOT / "sql/schema.sql").read_text())
    try:
        with ConnectionPool(
            dsn, min_size=1, max_size=4, timeout=10, open=True,
            kwargs={"row_factory": dict_row, "options": f"-c search_path={schema}"},
        ) as test_pool:
            monkeypatch.setattr(db, "pool", test_pool)
            monkeypatch.setattr(db.config, "DATABASE_URL", dsn)
            yield test_pool
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def invoice():
    """Load an exact Decimal invoice containing two line items."""
    data = json.loads((ROOT / "data/ground_truth/inv_006.json").read_text())
    data["field_confidence"] = {}
    return ExtractedInvoice.model_validate(data)


def test_document_and_items_commit_together(database, invoice):
    """Read committed values through a second connection, preserving order."""
    invoice.vendor_name = "Harbor's Services"
    invoice.field_confidence = {"vendor_name": 0.81, "total": 0.98}
    saved = db.save_document(ROOT / "data/invoices/inv_006.pdf", invoice)
    with database.connection() as connection:
        document = connection.execute("SELECT * FROM documents WHERE id = %s", (saved["id"],)).fetchone()
        items = connection.execute(
            "SELECT * FROM line_items WHERE document_id = %s ORDER BY line_number",
            (saved["id"],),
        ).fetchall()
    assert saved == {**document, "line_items": items}
    assert document["status"] == "received"
    assert document["vendor_name"] == "Harbor's Services"
    assert document["total"] == invoice.total
    assert document["confidence"] == Decimal("0.810")
    assert document["stored_path"] == "data/invoices/inv_006.pdf"
    assert db.get_document(saved["id"])["stored_path"] == document["stored_path"]
    assert db.list_documents()[0]["confidence"] == Decimal("0.810")
    assert [item["line_number"] for item in items] == [1, 2]
    assert [item["line_total"] for item in items] == [item.line_total for item in invoice.line_items]


def test_second_item_failure_rolls_back_parent_and_first_item(database, invoice):
    """A numeric overflow after the first item must leave both tables empty."""
    invoice.line_items[1].unit_price = Decimal("10000000000.00")
    with pytest.raises(psycopg.errors.NumericValueOutOfRange):
        db.save_document(ROOT / "data/invoices/inv_006.pdf", invoice)
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0
        assert connection.execute("SELECT count(*) AS n FROM line_items").fetchone()["n"] == 0
    invoice.line_items[1].unit_price = Decimal("875.50")
    assert len(db.save_document(ROOT / "data/invoices/inv_006.pdf", invoice)["line_items"]) == 2


def test_nullable_fields_and_empty_items_are_saved_as_received(database, invoice):
    """Store incomplete extraction results without applying business checks."""
    for field in ("vendor_name", "invoice_number", "invoice_date", "due_date", "currency", "subtotal", "tax", "total"):
        setattr(invoice, field, None)
    invoice.line_items = []
    saved = db.save_document(ROOT / "data/invoices/inv_006.pdf", invoice)
    assert saved["status"] == "received"
    assert saved["total"] is None
    assert saved["line_items"] == []


def test_concurrent_duplicate_runs_cannot_both_approve(database, invoice, monkeypatch):
    """Store both attempts while approving only the first committed invoice."""
    monkeypatch.setattr(run, "extract_fields", lambda text: (invoice, extract.LlmUsage("fixture", 100, 50)))
    path = ROOT / "data/invoices/inv_006.pdf"
    with ThreadPoolExecutor(max_workers=2) as executor:
        records = list(executor.map(run.process, [path, path]))
    assert sorted(record["status"] for record in records) == ["approved", "needs_review"]
    reviewed = next(record for record in records if record["status"] == "needs_review")
    assert [failure["code"] for failure in reviewed["failures"]] == ["DUPLICATE"]
    with database.connection() as connection:
        for record in records:
            logs = connection.execute(
                "SELECT * FROM processing_log WHERE document_id = %s ORDER BY id", (record["id"],)
            ).fetchall()
            assert [row["stage"] for row in logs] == ["ingest", "text", "extract", "validate", "route", "persist"]
            assert all(row["duration_ms"] >= 0 for row in logs)
            assert logs[2]["prompt_tokens"] == 100
            assert logs[2]["output_tokens"] == 50
            assert logs[3]["detail"]["failures"] == record["failures"]
        assert connection.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 2


def _mock_model(monkeypatch, invoice):
    response = SimpleNamespace(
        parsed=invoice.model_dump(mode="json"),
        usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=50, thoughts_token_count=10),
    )
    factory = MagicMock()
    generate = factory.return_value.__enter__.return_value.models.generate_content
    generate.return_value = response
    monkeypatch.setattr(extract.genai, "Client", factory)
    monkeypatch.setattr(extract.config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(extract.config, "LLM_MODEL", "test-model")
    monkeypatch.setattr(extract.time, "sleep", MagicMock())
    return generate, response


def test_http_retries_become_database_audit_rows(database, invoice, monkeypatch, caplog):
    """Persist each retry with its delay and the successful stage's usage."""
    generate, response = _mock_model(monkeypatch, invoice)
    generate.side_effect = [extract.errors.APIError(429, {}), extract.errors.APIError(503, {}), response]
    record = run.process(ROOT / "data/invoices/inv_006.pdf")
    assert record["status"] == "approved"
    with database.connection() as connection:
        logs = connection.execute(
            "SELECT * FROM processing_log WHERE document_id = %s AND stage = 'extract' ORDER BY id", (record["id"],)
        ).fetchall()
    assert [row["outcome"] for row in logs] == ["retry", "retry", "success"]
    assert [row["detail"]["delay_s"] for row in logs[:2]] == [1, 2]
    assert [row["detail"]["http_status"] for row in logs[:2]] == [429, 503]
    assert logs[-1]["prompt_tokens"] == 100
    assert logs[-1]["output_tokens"] == 60
    assert "outcome=retry" not in caplog.text


def test_exhausted_transient_failure_preserves_retry_audit(database, invoice, monkeypatch):
    """Keep retry history and received metadata when a 503 outlasts retries."""
    generate, _ = _mock_model(monkeypatch, invoice)
    generate.side_effect = extract.errors.APIError(503, {})
    with pytest.raises(extract.TransientExtractionError):
        run.process(ROOT / "data/invoices/inv_006.pdf")
    with database.connection() as connection:
        document = connection.execute("SELECT * FROM documents").fetchone()
        logs = connection.execute("SELECT * FROM processing_log WHERE stage = 'extract' ORDER BY id").fetchall()
        assert connection.execute("SELECT count(*) AS n FROM line_items").fetchone()["n"] == 0
    assert document["status"] == "received"
    assert document["vendor_name"] is None
    assert [row["outcome"] for row in logs] == ["retry", "retry", "retry", "transient_error"]
    assert [row["detail"]["delay_s"] for row in logs[:3]] == [1, 2, 4]


def test_invalid_schema_failure_stores_only_failed_metadata(database, invoice, monkeypatch):
    """Retain the schema retry and token usage after the second invalid response."""
    generate, response = _mock_model(monkeypatch, invoice)
    response.parsed = None
    record = run.process(ROOT / "data/invoices/inv_006.pdf")
    assert generate.call_count == 2
    assert record["status"] == "failed"
    assert record["vendor_name"] is None
    assert record["line_items"] == []
    with database.connection() as connection:
        logs = connection.execute("SELECT * FROM processing_log WHERE stage = 'extract' ORDER BY id").fetchall()
    assert [row["outcome"] for row in logs] == ["retry", "failed"]
    assert logs[-1]["prompt_tokens"] == 200
    assert logs[-1]["output_tokens"] == 120
    assert logs[-1]["detail"]["error_type"] == "ValidationError"


def test_persistence_failure_rolls_back_items_and_retains_failure_log(database, invoice, monkeypatch):
    """Save failed metadata and audit rows after the original transaction rolls back."""
    invoice.line_items[1].unit_price = Decimal("10000000000.00")
    monkeypatch.setattr(run, "extract_fields", lambda text: (invoice, extract.LlmUsage("fixture")))
    record = run.process(ROOT / "data/invoices/inv_006.pdf")
    assert record["status"] == "failed"
    assert record["line_items"] == []
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 1
        assert connection.execute("SELECT count(*) AS n FROM line_items").fetchone()["n"] == 0
        failure = connection.execute("SELECT detail FROM processing_log WHERE stage = 'persist' AND outcome = 'failed'").fetchone()
    assert failure["detail"]["error_type"] == "NumericValueOutOfRange"


@pytest.mark.parametrize("score", [0.60, float("nan")])
def test_low_confidence_is_stored_for_review(database, invoice, monkeypatch, score):
    """Keep unusable confidence auditable without a JSON serialization failure."""
    invoice.field_confidence = {"total": score}
    monkeypatch.setattr(run, "extract_fields", lambda text: (invoice, extract.LlmUsage("fixture")))
    record = run.process(ROOT / "data/invoices/inv_006.pdf")
    assert record["status"] == "needs_review"
    assert [failure["code"] for failure in record["failures"]] == ["LOW_CONFIDENCE"]
    with database.connection() as connection:
        detail = connection.execute("SELECT detail FROM processing_log WHERE stage = 'validate'").fetchone()["detail"]
    assert detail["failures"] == record["failures"]


def test_received_records_from_step4_are_duplicates(database, invoice, monkeypatch):
    """Include pre-validation records when checking the existing invoice pair."""
    path = ROOT / "data/invoices/inv_006.pdf"
    db.save_document(path, invoice)
    monkeypatch.setattr(run, "extract_fields", lambda text: (invoice, extract.LlmUsage("fixture")))
    record = run.process(path)
    assert record["status"] == "needs_review"
    assert [failure["code"] for failure in record["failures"]] == ["DUPLICATE"]
