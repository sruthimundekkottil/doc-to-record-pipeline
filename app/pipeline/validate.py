"""Check extracted invoices deterministically without model calls or database I/O."""

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app import config
from app.models import ExtractedInvoice


@dataclass(frozen=True)
class Failure:
    """A validation code and an explanation suitable for the audit log."""

    code: str
    message: str


def validate(inv: ExtractedInvoice, *, duplicate_exists: bool) -> list[Failure]:
    """Return every failed check, skipping arithmetic with absent operands."""
    failures = []
    line_sum = sum((item.line_total for item in inv.line_items), Decimal("0"))
    if inv.subtotal is not None and abs(line_sum - inv.subtotal) > Decimal("1.00"):
        failures.append(Failure("SUBTOTAL_MISMATCH", "Line totals differ from subtotal by more than 1.00."))
    if all(value is not None for value in (inv.subtotal, inv.tax, inv.total)):
        if abs(inv.subtotal + inv.tax - inv.total) > Decimal("1.00"):
            failures.append(Failure("TOTAL_MISMATCH", "Subtotal plus tax differs from total by more than 1.00."))
    for number, item in enumerate(inv.line_items, start=1):
        if abs(item.quantity * item.unit_price - item.line_total) > Decimal("0.05"):
            failures.append(Failure("LINE_MATH", f"Line {number} quantity times unit price differs from line total by more than 0.05."))
    missing = [name for name in ("vendor_name", "invoice_number") if not (getattr(inv, name) or "").strip()]
    if inv.total is None:
        missing.append("total")
    if missing:
        failures.append(Failure("MISSING_FIELD", "Missing required fields: " + ", ".join(missing)))
    if inv.invoice_date is None or inv.invoice_date > date.today():
        failures.append(Failure("BAD_DATE", "Invoice date is absent or in the future."))
    if inv.invoice_date is not None and inv.due_date is not None and inv.due_date < inv.invoice_date:
        failures.append(Failure("DATE_ORDER", "Due date precedes invoice date."))
    values = [inv.total]
    values.extend(value for item in inv.line_items for value in (item.quantity, item.unit_price, item.line_total))
    if any(value is not None and value < 0 for value in values):
        failures.append(Failure("NEGATIVE_VALUE", "A quantity, unit price, or total is negative."))
    low = [name for name, score in inv.field_confidence.items()
           if getattr(inv, name, False) is not None
           and (not math.isfinite(score) or not config.CONFIDENCE_THRESHOLD <= Decimal(str(score)) <= 1)]
    if low:
        failures.append(Failure("LOW_CONFIDENCE", "Low or invalid confidence for: " + ", ".join(low)))
    if duplicate_exists:
        failures.append(Failure("DUPLICATE", "This vendor and invoice number already exist."))
    return failures
