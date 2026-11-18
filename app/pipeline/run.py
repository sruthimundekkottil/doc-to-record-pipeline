"""Extract, validate, route, and persist invoices with a stage audit trail."""

import argparse
import json
import logging
import math
import time
from uuid import uuid4
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app import db
from app.pipeline.extract import TransientExtractionError, capture_extraction_audit, extract_fields
from app.pipeline.text import extract_text
from app.pipeline.validate import validate

ProcessResult = dict[str, Any]


def process_upload(stream, filename: str, content_type: str) -> ProcessResult:
    """Validate and retain a bounded PDF upload before processing it."""
    if content_type != "application/pdf":
        raise ValueError("Upload must have content type application/pdf.")
    content = stream.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("PDF exceeds the 10 MB upload limit.")
    if not content.startswith(b"%PDF-"):
        raise ValueError("Upload is not a valid PDF file.")
    name = Path(filename.replace("\\", "/")).name
    if name in ("", ".", ".."):
        name = "invoice.pdf"
    directory = Path(__file__).resolve().parents[2] / "data" / "stored" / uuid4().hex
    directory.mkdir(parents=True)
    path = directory / name
    try:
        path.write_bytes(content)
    except OSError:
        path.unlink(missing_ok=True)
        directory.rmdir()
        raise
    return process(path)


@contextmanager
def _stage(events, name):
    event = {"stage": name, "outcome": "success", "detail": {}}
    started = time.perf_counter()
    try:
        yield event
    except Exception as error:
        event["outcome"] = "transient_error" if isinstance(error, TransientExtractionError) else "failed"
        event["detail"]["error_type"] = type(error).__name__
        if isinstance(error, ValidationError):
            event["detail"]["errors"] = error.errors(include_input=False, include_context=False, include_url=False)
        raise
    finally:
        event["duration_ms"] = round((time.perf_counter() - started) * 1000)
        events.append(event)


def _finalize(pdf_path, invoice, events):
    with db.transaction() as connection:
        with _stage(events, "validate") as event:
            failures = validate(invoice, duplicate_exists=db.duplicate_exists(connection, invoice))
            failure_details = [asdict(failure) for failure in failures]
            event["outcome"] = "needs_review" if failures else "passed"
            event["detail"]["failures"] = failure_details
        with _stage(events, "route") as event:
            status = "needs_review" if failures else "approved"
            event["detail"]["status"] = status
        record = db.save_document(
            pdf_path, invoice, status=status, events=events, connection=connection,
        )
    record["failures"] = failure_details
    return record


def process(pdf_path: Path) -> ProcessResult:
    """Return a committed invoice and failures, preserving transient API errors."""
    events = []
    started = time.perf_counter()
    try:
        with _stage(events, "ingest"):
            pdf_path = pdf_path.resolve(strict=True)
        with _stage(events, "text") as event:
            text, method = extract_text(pdf_path)
            event["detail"]["method"] = method
        with _stage(events, "extract") as event:
            with capture_extraction_audit(events) as usage:
                try:
                    invoice, usage = extract_fields(text)
                    event["detail"].update(
                        field_confidence={name: score if math.isfinite(score) else str(score)
                                          for name, score in invoice.field_confidence.items()},
                    )
                finally:
                    event["detail"]["usage_scope"] = "all_attempts"
                    event.update(model=usage.model, prompt_tokens=usage.prompt_tokens, output_tokens=usage.output_tokens)
        started = time.perf_counter()
        return _finalize(pdf_path, invoice, events)
    except Exception as error:
        if events[-1]["outcome"] not in ("failed", "transient_error"):
            events.append({"stage": "persist", "outcome": "failed",
                           "detail": {"error_type": type(error).__name__},
                           "duration_ms": round((time.perf_counter() - started) * 1000)})
        with _stage(events, "route") as event:
            status = "received" if isinstance(error, TransientExtractionError) else "failed"
            event["detail"]["status"] = status
        record = db.save_document(pdf_path, None, status=status, events=events)
        if isinstance(error, TransientExtractionError):
            raise
        record["failures"] = [failure for event in events if event["stage"] == "validate"
                              for failure in event["detail"].get("failures", [])]
        return record


def main() -> None:
    """Process one PDF and print its stored document and line items."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    try:
        record = process(args.pdf_path)
    except TransientExtractionError as error:
        logging.getLogger(__name__).error("stage=extract outcome=transient_error %s", error)
        raise SystemExit(75) from error
    finally:
        db.pool.close()
    print(json.dumps(record, indent=2, default=str))
    if record["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
