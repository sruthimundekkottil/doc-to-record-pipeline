"""Check read-only evaluation comparisons and metric denominators."""

import json
from copy import deepcopy
from datetime import date
from decimal import Decimal

from scripts import eval


def _fixture():
    truth = json.loads((eval.TRUTH_DIR / "inv_001.json").read_text())
    record = {**deepcopy(truth), "source_filename": "inv_001.pdf",
              "status": "approved", "automatic_status": "approved",
              "confidence": Decimal("0.800")}
    return record, truth


def test_field_comparisons_and_boundaries():
    """Normalize vendor names, compare dates and Decimal amounts precisely."""
    record, truth = _fixture()
    record["vendor_name"] = "  CEDAR\n Consulting  "
    record["invoice_date"] = date.fromisoformat(truth["invoice_date"])
    record["total"] = Decimal(truth["total"]) + Decimal("0.01")
    assert all(eval.compare(record, truth).values())
    record["total"] += Decimal("0.001")
    record["invoice_number"] += " "
    record["line_items"][0]["description"] += " extra"
    result = eval.compare(record, truth)
    assert not result["total"] and not result["invoice_number"] and not result["line_items"]
    assert result["line_items_count"]
    record["line_items"] = []
    assert not eval.compare(record, truth)["line_items_count"]


def test_item_comparison_normalises_text_and_numeric_scale():
    """Ignore case, whitespace and numeric scale, preserving real differences."""
    record, truth = _fixture()
    item = record["line_items"][0]
    item["description"] = "  CONSULTING\n session   - PHASE 1  "
    item.update(quantity=Decimal("1.000"), unit_price=Decimal("1250.0"),
                line_total=Decimal("1250.000"))
    assert eval.compare(record, truth)["line_items"]
    for field in ("quantity", "unit_price", "line_total"):
        item[field] += Decimal("0.001")
        assert not eval.compare(record, truth)["line_items"]
        item[field] -= Decimal("0.001")
    item["description"] = "Consulting session phase 1"
    assert not eval.compare(record, truth)["line_items"]


def test_report_uses_original_routing_and_non_null_confidence():
    """Human approvals do not increase auto-approval or erase false flags."""
    first, truth = _fixture()
    second = {**first, "source_filename": "inv_002.pdf", "automatic_status": "needs_review",
              "confidence": Decimal("0.600")}
    third = {**first, "source_filename": "inv_003.pdf", "automatic_status": "needs_review",
             "confidence": None}
    truths = {f"inv_{n:03}.pdf": truth for n in range(1, 5)}
    output = eval.report([first, second, third], truths, {"inv_002.pdf": {}})
    assert "| Auto-approval rate | 33.33% | 1/3" in output
    assert "| Flagging precision | 50.00% | 1/2" in output
    assert "| Mean confidence | 0.700 | 2 non-null" in output
    assert "| Stored fixture coverage | 3/4" in output


def test_empty_report_and_metadata_only_attempts():
    """Avoid division by zero and false credit for failed or pending extraction."""
    record, truth = _fixture()
    record["status"] = "received"
    assert not any(eval.compare(record, truth).values())
    output = eval.report([], {"inv_001.pdf": truth}, {})
    assert "| Auto-approval rate | N/A |" in output
    assert "| Flagging precision | N/A |" in output
    assert "| Mean confidence | N/A |" in output
    assert "Mean logged latency" not in output
    assert "Mean tokens" not in output


def test_report_includes_available_audit_measurements():
    """Average available measurements without treating missing logs as zero."""
    record, truth = _fixture()
    record.update(latency_ms=1200, tokens=100)
    other = {**record, "source_filename": "inv_002.pdf", "latency_ms": None, "tokens": None}
    output = eval.report([record, other], {"inv_001.pdf": truth, "inv_002.pdf": truth}, {})
    assert "| Mean logged latency (ms) | 1200.00 | 1 records" in output
    assert "| Mean tokens | 100.00 | 1 records" in output


def test_main_reads_stored_data_and_prints_two_tables(monkeypatch, capsys):
    """Exercise the command without a model client or database writes."""
    record, _ = _fixture()
    monkeypatch.setattr(eval.db, "evaluation_documents", lambda filenames: [record])
    monkeypatch.setattr(eval.db.pool, "close", lambda: None)
    eval.main()
    output = capsys.readouterr().out
    assert sum(line.startswith("| ---") for line in output.splitlines()) == 2
    assert "| vendor_name | 1 | 1 | 100.00% |" in output
