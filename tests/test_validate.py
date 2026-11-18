"""Exercise all validation codes, nullable inputs, and exact tolerance boundaries."""

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app import config
from app.models import ExtractedInvoice
from app.pipeline.validate import validate


@pytest.fixture
def invoice():
    """Provide a clean invoice independent of model confidence variation."""
    path = Path(__file__).resolve().parents[1] / "data/ground_truth/inv_001.json"
    data = json.loads(path.read_text())
    data["field_confidence"] = {"total": 0.99}
    data["invoice_date"] = "2020-01-01"
    data["due_date"] = "2020-01-31"
    return ExtractedInvoice.model_validate(data)


def test_clean_invoice_has_no_failures(invoice):
    """Allow a complete invoice whose checks all pass."""
    assert validate(invoice, duplicate_exists=False) == []


@pytest.mark.parametrize("tax_confidence", [{}, {"tax": 0.0}])
def test_no_tax_invoice_has_no_confidence_failure(invoice, tax_confidence):
    """Absent tax needs no confidence score, even if extraction supplies zero."""
    invoice.tax = None
    invoice.total = invoice.subtotal
    invoice.field_confidence.update(tax_confidence)
    assert validate(invoice, duplicate_exists=False) == []


def test_missing_required_field_is_not_a_confidence_failure(invoice):
    """Report missing required values through the presence check only."""
    invoice.vendor_name = None
    invoice.field_confidence["vendor_name"] = 0.0
    assert [failure.code for failure in validate(invoice, duplicate_exists=False)] == ["MISSING_FIELD"]


@pytest.mark.parametrize("field,value,code", [
    ("subtotal", Decimal("1200.00"), "SUBTOTAL_MISMATCH"),
    ("total", Decimal("1500.00"), "TOTAL_MISMATCH"),
    ("vendor_name", "  ", "MISSING_FIELD"),
    ("invoice_number", None, "MISSING_FIELD"),
    ("invoice_date", None, "BAD_DATE"),
    ("due_date", date(2019, 12, 31), "DATE_ORDER"),
    ("total", Decimal("-1.00"), "NEGATIVE_VALUE"),
    ("field_confidence", {"total": 0.69}, "LOW_CONFIDENCE"),
])
def test_validation_codes(invoice, field, value, code):
    """Report each failing check even when another check also fails."""
    setattr(invoice, field, value)
    assert code in {failure.code for failure in validate(invoice, duplicate_exists=False)}


def test_future_invoice_date(invoice):
    """Reject tomorrow while allowing today's invoice date."""
    invoice.invoice_date = date.today() + timedelta(days=1)
    invoice.due_date = None
    assert "BAD_DATE" in {failure.code for failure in validate(invoice, duplicate_exists=False)}
    invoice.invoice_date = date.today()
    assert validate(invoice, duplicate_exists=False) == []


def test_line_math_and_duplicate(invoice):
    """Collect a line arithmetic failure alongside a duplicate failure."""
    invoice.line_items[0].quantity = Decimal("2")
    assert {failure.code for failure in validate(invoice, duplicate_exists=True)} == {"LINE_MATH", "DUPLICATE"}


def test_tolerance_boundaries_are_inclusive(invoice):
    """Accept differences of exactly 0.05 and 1.00 and confidence of 0.70."""
    invoice.line_items[0].line_total = Decimal("1250.05")
    invoice.subtotal = Decimal("1251.05")
    invoice.total = Decimal("1477.05")
    invoice.field_confidence = {"total": 0.70}
    assert validate(invoice, duplicate_exists=False) == []
    invoice.line_items[0].line_total = Decimal("1250.0501")
    assert "LINE_MATH" in {failure.code for failure in validate(invoice, duplicate_exists=False)}


def test_missing_arithmetic_operands_do_not_raise(invoice):
    """Retain nullable operands while flagging explicitly required fields."""
    invoice.subtotal = invoice.tax = invoice.total = None
    invoice.invoice_date = None
    assert {failure.code for failure in validate(invoice, duplicate_exists=False)} == {"MISSING_FIELD", "BAD_DATE"}


def test_all_nine_codes_can_be_reported_together(invoice):
    """Run all checks even after an early validation failure."""
    invoice.vendor_name = None
    invoice.invoice_date = date.today() + timedelta(days=1)
    invoice.subtotal = Decimal("1.00")
    invoice.total = Decimal("-1.00")
    invoice.line_items[0].quantity = Decimal("-1")
    invoice.field_confidence = {"total": 0.1}
    assert {failure.code for failure in validate(invoice, duplicate_exists=True)} == {
        "SUBTOTAL_MISMATCH", "TOTAL_MISMATCH", "LINE_MATH", "MISSING_FIELD",
        "BAD_DATE", "DATE_ORDER", "NEGATIVE_VALUE", "LOW_CONFIDENCE", "DUPLICATE",
    }


def test_confidence_threshold_is_configurable(invoice, monkeypatch):
    """Use the named configuration threshold on every validation call."""
    invoice.field_confidence = {"total": 0.8}
    monkeypatch.setattr(config, "CONFIDENCE_THRESHOLD", Decimal("0.90"))
    assert [failure.code for failure in validate(invoice, duplicate_exists=False)] == ["LOW_CONFIDENCE"]


@pytest.mark.parametrize("score", [float("nan"), float("inf"), 1.1])
def test_invalid_confidence_is_flagged(invoice, score):
    """Treat nonfinite or out-of-range confidence as unusable."""
    invoice.field_confidence = {"total": score}
    assert [failure.code for failure in validate(invoice, duplicate_exists=False)] == ["LOW_CONFIDENCE"]
