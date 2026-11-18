"""Own the PostgreSQL pool and transactional document persistence."""

import atexit
import json
import math
import time
from decimal import Decimal
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app import config
from app.models import ExtractedInvoice

ROOT = Path(__file__).resolve().parents[1]


def _confidence(scores):
    if not scores or any(not math.isfinite(float(score)) for score in scores.values()):
        return None
    # The weakest field drives review routing, so retain the minimum confidence.
    return min(Decimal(str(score)) for score in scores.values())

pool = ConnectionPool(
    config.DATABASE_URL,
    open=False,
    min_size=1,
    max_size=4,
    timeout=10,
    kwargs={"row_factory": dict_row, "connect_timeout": 10},
)
atexit.register(pool.close)

INSERT_DOCUMENT = """
    INSERT INTO documents (
        id, source_filename, stored_path, status, vendor_name, invoice_number,
        invoice_date, due_date, currency, subtotal, tax, total, confidence
    ) VALUES (
        %(id)s, %(source_filename)s, %(stored_path)s, %(status)s, %(vendor_name)s,
        %(invoice_number)s, %(invoice_date)s, %(due_date)s, %(currency)s,
        %(subtotal)s, %(tax)s, %(total)s, %(confidence)s
    ) RETURNING *
"""
INSERT_LINE_ITEM = """
    INSERT INTO line_items (
        document_id, line_number, description, quantity, unit_price, line_total
    ) VALUES (%s, %s, %s, %s, %s, %s)
"""


@contextmanager
def transaction(connection=None):
    """Reuse a caller's transaction or commit/roll back a pooled connection."""
    if connection is not None:
        yield connection
        return
    if not config.DATABASE_URL:
        raise ValueError("Set DATABASE_URL in the environment or .env.")
    pool.open(wait=False)
    with pool.connection() as connection:
        yield connection


def duplicate_exists(connection, invoice: ExtractedInvoice) -> bool:
    """Serialize each invoice's duplicate check and subsequent insert."""
    if not invoice.vendor_name or not invoice.invoice_number:
        return False
    key = json.dumps([invoice.vendor_name, invoice.invoice_number])
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
    return connection.execute(
        "SELECT EXISTS (SELECT 1 FROM documents WHERE vendor_name = %s AND invoice_number = %s) AS found",
        (invoice.vendor_name, invoice.invoice_number),
    ).fetchone()["found"]


def _write_logs(connection, document_id, events):
    with connection.cursor() as cursor:
        cursor.executemany("""
            INSERT INTO processing_log (
                document_id, stage, outcome, detail, duration_ms,
                model, prompt_tokens, output_tokens
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, [
            (document_id, event["stage"], event["outcome"], Jsonb(event.get("detail", {})),
             event["duration_ms"], event.get("model"), event.get("prompt_tokens"),
             event.get("output_tokens"))
            for event in events
        ])


def save_document(
    pdf_path: Path, invoice: ExtractedInvoice | None, *, status="received",
    events: list[dict] | None = None, connection=None,
) -> dict[str, Any]:
    """Atomically save the document, its items, and any stage/retry audit rows."""
    started = time.perf_counter()
    parameters = dict.fromkeys(ExtractedInvoice.model_fields)
    if invoice is not None:
        parameters.update(invoice.model_dump(exclude={"line_items", "field_confidence"}))
    parameters.update(
        id=uuid4(), source_filename=pdf_path.name,
        stored_path=pdf_path.resolve().relative_to(ROOT).as_posix(), status=status,
        confidence=_confidence(invoice.field_confidence) if invoice is not None else None,
    )
    with transaction(connection) as connection:
        document = connection.execute(INSERT_DOCUMENT, parameters).fetchone()
        items = invoice.line_items if invoice is not None else []
        with connection.cursor() as cursor:
            cursor.executemany(INSERT_LINE_ITEM, [
                (parameters["id"], number, item.description, item.quantity,
                 item.unit_price, item.line_total)
                for number, item in enumerate(items, start=1)
            ])
        document["line_items"] = connection.execute(
            "SELECT * FROM line_items WHERE document_id = %s ORDER BY line_number",
            (parameters["id"],),
        ).fetchall()
        if events is not None:
            persisted = {"stage": "persist", "outcome": "success", "detail": {"status": status},
                         "duration_ms": round((time.perf_counter() - started) * 1000)}
            _write_logs(connection, parameters["id"], [*events, persisted])
    return document


def migrate_document_metadata():
    """Normalize existing paths and recover confidence from extraction audit rows."""
    with transaction() as connection:
        documents = connection.execute("SELECT id, stored_path FROM documents FOR UPDATE").fetchall()
        for document in documents:
            path = Path(document["stored_path"])
            relative = path.relative_to(ROOT).as_posix() if path.is_absolute() else path.as_posix()
            log = connection.execute(
                "SELECT detail FROM processing_log WHERE document_id = %s AND stage = 'extract' AND outcome = 'success' ORDER BY id DESC LIMIT 1",
                (document["id"],),
            ).fetchone()
            scores = log["detail"].get("field_confidence", {}) if log else {}
            connection.execute(
                "UPDATE documents SET stored_path = %s, confidence = %s WHERE id = %s",
                (relative, _confidence(scores), document["id"]),
            )


def get_document(document_id, connection=None):
    """Return a stored document, ordered items, and validation failures."""
    with transaction(connection) as connection:
        document = connection.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
        if document is None:
            return None
        document["line_items"] = connection.execute(
            "SELECT * FROM line_items WHERE document_id = %s ORDER BY line_number", (document_id,)
        ).fetchall()
        log = connection.execute(
            "SELECT detail FROM processing_log WHERE document_id = %s AND stage = 'validate' ORDER BY id DESC LIMIT 1",
            (document_id,),
        ).fetchone()
        document["failures"] = log["detail"].get("failures", []) if log else []
        return document


def list_documents(status=None, limit=20, offset=0):
    """List document summaries in stable creation order with optional filtering."""
    with transaction() as connection:
        return connection.execute(
            "SELECT * FROM documents WHERE (%s::text IS NULL OR status = %s) ORDER BY created_at DESC, id LIMIT %s OFFSET %s",
            (status, status, limit, offset),
        ).fetchall()


def approve_document(document_id):
    """Approve a flagged record and atomically record the human override."""
    started = time.perf_counter()
    with transaction() as connection:
        document = connection.execute("SELECT status FROM documents WHERE id = %s FOR UPDATE", (document_id,)).fetchone()
        if document is None:
            return None
        if document["status"] != "needs_review":
            raise ValueError("Only a needs_review document can be approved.")
        connection.execute("UPDATE documents SET status = 'approved' WHERE id = %s", (document_id,))
        _write_logs(connection, document_id, [{
            "stage": "approve", "outcome": "success", "detail": {"action": "human_override", "previous_status": "needs_review"},
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }])
        return get_document(document_id, connection)


def health():
    """Report database connectivity and model-key configuration without secrets."""
    from psycopg import Error
    from psycopg_pool import PoolTimeout
    try:
        with transaction() as connection:
            connection.execute("SELECT 1")
        database = True
    except (Error, PoolTimeout, ValueError):
        database = False
    return {"database": database, "model_api_key_configured": bool(config.LLM_API_KEY.strip())}
