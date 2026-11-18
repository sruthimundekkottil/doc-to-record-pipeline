"""Check fixture diversity, structural layouts, and documented broken cases."""

import json
from decimal import Decimal
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]


def test_generated_corpus():
    """Verify all pairs and the three deliberate anomalies."""
    pdfs = sorted((ROOT / "data/invoices").glob("inv_*.pdf"))
    assert len(pdfs) == 30
    prices, descriptions = set(), set()
    for path in pdfs:
        truth = json.loads((ROOT / "data/ground_truth" / (path.stem + ".json")).read_text())
        for item in truth["line_items"]:
            prices.add(item["unit_price"])
            descriptions.add(item["description"])
        subtotal = sum((Decimal(item["line_total"]) for item in truth["line_items"]), Decimal("0"))
        assert (subtotal != Decimal(truth["subtotal"])) == (path.stem == "inv_028")
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        assert "Tax (no tax)" not in text
        if path.stem == "inv_013":
            assert text.index("Subtotal") < text.index("Description")
        if path.stem == "inv_019":
            assert "Qty" not in text and "Fixed-fee services" in text
    assert len(prices) > 30 and len(descriptions) > 30
    assert (ROOT / "data/invoices/inv_030.pdf").read_bytes() == pdfs[0].read_bytes()
    assert json.loads((ROOT / "data/ground_truth/inv_029.json").read_text())["vendor_name"] is None
    assert len(json.loads((ROOT / "data/ground_truth/broken.json").read_text())) == 3
