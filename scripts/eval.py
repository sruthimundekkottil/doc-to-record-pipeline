"""Compare stored PostgreSQL records with fixtures without running the pipeline.

Run from the project root with: python -m scripts.eval
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app import db

TRUTH_DIR = Path(__file__).resolve().parents[1] / "data/ground_truth"
FIELDS = ("vendor_name", "invoice_number", "invoice_date", "due_date", "total",
          "line_items_count", "line_items")


def _vendor(value):
    return " ".join(value.casefold().split()) if value is not None else None


def _date(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


def _items(items):
    return [(item["description"], *(Decimal(item[key]) if item[key] is not None else None
             for key in ("quantity", "unit_price", "line_total"))) for item in items]


def compare(record, truth):
    """Compare individual fields, requiring equal counts before ordered item matches."""
    total, expected = record["total"], truth["total"]
    matches = {
        "vendor_name": _vendor(record["vendor_name"]) == _vendor(truth["vendor_name"]),
        "invoice_number": record["invoice_number"] == truth["invoice_number"],
        "invoice_date": _date(record["invoice_date"]) == _date(truth["invoice_date"]),
        "due_date": _date(record["due_date"]) == _date(truth["due_date"]),
        "total": total is expected if total is None or expected is None else
                 abs(Decimal(total) - Decimal(expected)) <= Decimal("0.01"),
        "line_items_count": len(record["line_items"]) == len(truth["line_items"]),
    }
    matches["line_items"] = matches["line_items_count"] and _items(record["line_items"]) == _items(truth["line_items"])
    # Metadata-only attempts must not earn accuracy credit for matching nulls.
    return {field: matched and record["status"] in ("approved", "needs_review")
            for field, matched in matches.items()}


def _rate(numerator, denominator):
    return f"{Decimal(numerator) / denominator:.2%}" if denominator else "N/A"


def report(records, truths, broken):
    """Return two Markdown tables with explicit denominators and fixture coverage."""
    results = [compare(record, truths[record["source_filename"]]) for record in records]
    lines = ["| Field | Correct | Evaluated | Accuracy |", "| --- | ---: | ---: | ---: |"]
    for field in FIELDS:
        correct = sum(result[field] for result in results)
        lines.append(f"| {field} | {correct} | {len(records)} | {_rate(correct, len(records))} |")
    approved = sum(record["automatic_status"] == "approved" for record in records)
    flagged = [record for record in records if record["automatic_status"] == "needs_review"]
    true_flags = sum(record["source_filename"] in broken for record in flagged)
    scores = [record["confidence"] for record in records if record["confidence"] is not None]
    mean = f"{sum(scores, Decimal('0')) / len(scores):.3f}" if scores else "N/A"
    lines += ["", "| System behaviour | Value | Basis |", "| --- | ---: | --- |",
              f"| Stored fixture coverage | {len(records)}/{len(truths)} | Latest attempt per filename |",
              f"| Missing stored fixtures | {len(truths) - len(records)} | Excluded from accuracy denominators |",
              f"| Received / failed | {sum(r['status'] == 'received' for r in records)} / {sum(r['status'] == 'failed' for r in records)} | Included as incorrect in field accuracy |",
              f"| Auto-approval rate | {_rate(approved, len(records))} | {approved}/{len(records)} stored fixtures; original route |",
              f"| Flagging precision | {_rate(true_flags, len(flagged))} | {true_flags}/{len(flagged)} flagged fixtures are in broken.json |",
              f"| Mean confidence | {mean} | {len(scores)} non-null stored confidence values |"]
    return "\n".join(lines)


def main():
    """Load truth and a read-only database snapshot, then print evaluation tables."""
    truths = {path.with_suffix(".pdf").name: json.loads(path.read_text(), parse_float=Decimal)
              for path in sorted(TRUTH_DIR.glob("inv_*.json"))}
    broken = json.loads((TRUTH_DIR / "broken.json").read_text())
    try:
        print(report(db.evaluation_documents(truths), truths, broken))
    finally:
        db.pool.close()


if __name__ == "__main__":
    main()
