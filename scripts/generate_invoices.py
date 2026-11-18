"""Generate thirty reproducible invoices in five layouts and matched truth."""

import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.pdfgen.canvas import Canvas


ROOT = Path(__file__).resolve().parents[1]
CENT = Decimal("0.01")
LINE_COUNTS = (1, 2, 3, 5, 8, 2, 4, 6, 9, 12)
VENDORS = ("Cedar Consulting", "Harbor Services", "Summit Analytics", "Orchid Studio", "Willow Systems")
BUYERS = ("Maple Research", "Lotus Retail", "Beacon Labs", "Juniper Foods", "Cobalt Logistics", "Elm Education")
CATALOG = (
    ("Consulting session", "1250.00"),
    ("Technical documentation", "875.50"),
    ("Data preparation", "640.25"),
    ("Quality assurance review", "425.75"),
    ("Deployment support", "1500.00"),
    ("Training materials", "225.50"),
    ("Design consultation", "950.00"),
    ("Monthly maintenance", "375.25"),
    ("Research summary", "720.50"),
    ("Process review", "560.75"),
    ("Configuration service", "810.25"),
    ("Handover session", "450.00"),
)
DATE_FORMATS = ("%d/%m/%Y", "%d-%b-%Y", "%Y-%m-%d", "%B %d, %Y")
INVOICE_LABELS = ("Invoice No.", "Invoice #", "Bill Number", "Ref")


def _make_invoice(number):
    items = []
    for index in range(LINE_COUNTS[(number - 1) % len(LINE_COUNTS)]):
        description, price = CATALOG[(index + number - 1) % len(CATALOG)]
        description = f"{description} - phase {number}"
        quantity = Decimal("1") if _layout(number) == 3 else Decimal(1 + (number + index - 1) % 3)
        unit_price = Decimal(price) + Decimal(number - 1) * Decimal("17.25")
        items.append({
            "description": description,
            "quantity": quantity,
            "unit_price": unit_price,
            "line_total": (quantity * unit_price).quantize(CENT),
        })
    subtotal = sum((item["line_total"] for item in items), Decimal("0.00"))
    tax_rates = (
        (("GST 18%", Decimal("0.18")),),
        (("CGST 9%", Decimal("0.09")), ("SGST 9%", Decimal("0.09"))),
        (),
    )[(number - 1) % 3]
    taxes = [
        (label, (subtotal * rate).quantize(CENT, rounding=ROUND_HALF_UP))
        for label, rate in tax_rates
    ]
    tax = sum((amount for _, amount in taxes), Decimal("0.00"))
    issued = date(2026, 3, 1) + timedelta(days=number - 1)
    invoice = {
        "vendor_name": VENDORS[_layout(number)],
        "invoice_number": f"INV-2026-{number:03d}",
        "invoice_date": issued.isoformat(),
        "due_date": (issued + timedelta(days=30)).isoformat(),
        "currency": "INR",
        "line_items": items,
        "subtotal": subtotal,
        "tax": tax if taxes else None,
        "total": subtotal + tax,
    }
    return invoice, taxes


def _check_invoice(invoice):
    # Catch fixture arithmetic errors before writing either member of a pair.
    items = invoice["line_items"]
    assert all(item["quantity"] * item["unit_price"] == item["line_total"] for item in items)
    assert sum((item["line_total"] for item in items), Decimal("0.00")) == invoice["subtotal"]
    assert invoice["subtotal"] + (invoice["tax"] or Decimal("0")) == invoice["total"]
    assert date.fromisoformat(invoice["due_date"]) >= date.fromisoformat(invoice["invoice_date"])


def _layout(number):
    return ((number - 1) // 6) % 5


def _draw_totals(canvas, invoice, taxes, x, y):
    canvas.setFillColor(HexColor("#252525"))
    for label, amount in [("Subtotal", invoice["subtotal"]), *taxes, ("Total", invoice["total"])]:
        canvas.setFont("Helvetica-Bold" if label == "Total" else "Helvetica", 11)
        canvas.drawString(x, y, label)
        canvas.drawRightString(x + 190, y, f"{amount:.2f}")
        y -= 22


def _draw_header(canvas, invoice, taxes, number, page):
    layout = _layout(number)
    classic = layout in (0, 2, 4)
    accent = HexColor("#24485C" if classic else "#86462D")
    canvas.setFillColor(accent)
    if invoice["vendor_name"]:
        canvas.rect(48 if classic else 507, 744, 40, 40, fill=1, stroke=0)
    canvas.setFillColor(HexColor("#FFFFFF"))
    canvas.setFont("Helvetica-Bold", 16)
    if invoice["vendor_name"]:
        canvas.drawCentredString(68 if classic else 527, 757, "".join(word[0] for word in invoice["vendor_name"].split()))
    canvas.setFillColor(accent)
    canvas.setFont("Helvetica-Bold", 22)
    if classic:
        canvas.drawString(104, 760, invoice["vendor_name"] or "")
        canvas.drawString(48, 690, "INVOICE")
    else:
        canvas.drawRightString(491, 760, invoice["vendor_name"] or "")
        canvas.drawString(48, 714, "BILL FOR SERVICES")
    canvas.setFillColor(HexColor("#252525"))
    canvas.setFont("Helvetica", 11)
    detail_y = 652 if classic else 676
    label = INVOICE_LABELS[(number - 1) % len(INVOICE_LABELS)]
    canvas.drawString(48, detail_y, f"{label} {invoice['invoice_number']}")
    date_format = DATE_FORMATS[(number - 1) % len(DATE_FORMATS)]
    issued = date.fromisoformat(invoice["invoice_date"]).strftime(date_format)
    due = date.fromisoformat(invoice["due_date"]).strftime(date_format)
    canvas.drawString(48, detail_y - 20, f"Invoice date: {issued}")
    canvas.drawString(48, detail_y - 40, f"Due date: {due}")
    canvas.drawString(350 if classic else 48, 652 if classic else 580, f"Bill to: {BUYERS[(number - 1) % len(BUYERS)]}")
    canvas.drawString(350 if classic else 48, 632 if classic else 560, "Chennai, India")
    canvas.setFont("Helvetica", 9)
    canvas.drawString(48, 44, f"{invoice['invoice_number']}  |  Synthetic sample invoice")
    canvas.drawRightString(547, 44, f"Page {page}")
    y = 566 if classic else 508
    if layout == 2:
        if page == 1:
            _draw_totals(canvas, invoice, taxes, 340, 570)
        y = 425
    if layout == 4:
        y = 520
    if layout == 3:
        canvas.setFont("Helvetica", 10)
        canvas.drawString(48, 548, "Fixed-fee services; each listed fee covers one engagement.")
    canvas.setFillColor(accent)
    canvas.rect(48, y, 499, 28, fill=1, stroke=0)
    canvas.setFillColor(HexColor("#FFFFFF"))
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(58, y + 10, "Description")
    if layout != 3:
        canvas.drawRightString(345, y + 10, "Qty")
    currency_label = "INR" if classic else "Rs."
    if layout != 3:
        canvas.drawRightString(435, y + 10, f"Rate ({currency_label})")
    canvas.drawRightString(537, y + 10, f"Amount ({currency_label})")
    return y - 26


def _render_pdf(invoice, taxes, number):
    output = BytesIO()
    canvas = Canvas(output, pagesize=(595, 842), invariant=1)
    canvas.setTitle(f"Invoice {invoice['invoice_number']}")
    canvas.setAuthor(invoice["vendor_name"] or "")
    page = 1
    y = _draw_header(canvas, invoice, taxes, number, page)
    layout = _layout(number)
    row_height = 52 if layout == 4 else 34
    for item in invoice["line_items"]:
        if y < 190:
            canvas.showPage()
            page += 1
            y = _draw_header(canvas, invoice, taxes, number, page)
        canvas.setFillColor(HexColor("#252525"))
        canvas.setFont("Helvetica", 9)
        description = item["description"]
        canvas.drawString(58, y, description)
        if layout != 3:
            canvas.drawRightString(345, y, str(item["quantity"]))
            canvas.drawRightString(435, y, f"{item['unit_price']:.2f}")
        canvas.drawRightString(537, y, f"{item['line_total']:.2f}")
        canvas.setStrokeColor(HexColor("#DDDDDD"))
        canvas.line(48, y - 18, 547, y - 18)
        y -= row_height
    if layout != 2:
        _draw_totals(canvas, invoice, taxes, 350 if layout == 0 else 58, y - 8)
    canvas.save()
    return output.getvalue()


def main():
    """Write thirty PDF/JSON pairs, including three identified broken cases."""
    pdf_directory = ROOT / "data" / "invoices"
    truth_directory = ROOT / "data" / "ground_truth"
    pdf_directory.mkdir(parents=True, exist_ok=True)
    truth_directory.mkdir(parents=True, exist_ok=True)
    for number in range(1, 31):
        invoice, taxes = _make_invoice(number)
        _check_invoice(invoice)
        if number == 28:
            invoice["subtotal"] += Decimal("100.00")
            invoice["total"] += Decimal("100.00")
        if number == 29:
            invoice["vendor_name"] = None
        if number == 30:
            (pdf_directory / "inv_030.pdf").write_bytes((pdf_directory / "inv_001.pdf").read_bytes())
            (truth_directory / "inv_030.json").write_bytes((truth_directory / "inv_001.json").read_bytes())
            continue
        pdf = _render_pdf(invoice, taxes, number)
        # Decimal strings preserve exact values when JSON is read by other tools.
        ground_truth = json.dumps(invoice, indent=2, default=str) + "\n"
        stem = f"inv_{number:03d}"
        (pdf_directory / f"{stem}.pdf").write_bytes(pdf)
        (truth_directory / f"{stem}.json").write_text(ground_truth, encoding="utf-8")
    broken = {
        "inv_028.pdf": {"expected_failures": ["SUBTOTAL_MISMATCH"]},
        "inv_029.pdf": {"expected_failures": ["MISSING_FIELD"], "missing_field": "vendor_name"},
        "inv_030.pdf": {"expected_failures": ["DUPLICATE"], "duplicate_of": "inv_001.pdf"},
    }
    (truth_directory / "broken.json").write_text(json.dumps(broken, indent=2) + "\n", encoding="utf-8")
    print("Generated 30 invoices in 5 layouts, including 3 broken documents.")


if __name__ == "__main__":
    main()
