"""Check native text across pages and the OCR threshold and cleanup."""

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from app.pipeline import text


@pytest.mark.parametrize("page_texts, method", [(["a" * 49, None], "ocr"), (["a" * 25] * 2, "native")])
def test_ocr_threshold_counts_all_pages(monkeypatch, page_texts, method):
    """Apply the 50-character threshold to page contents without separators."""
    pdf = MagicMock()
    pdf.__enter__.return_value.pages = [
        MagicMock(extract_text=MagicMock(return_value=value)) for value in page_texts
    ]
    monkeypatch.setattr(text.pdfplumber, "open", MagicMock(return_value=pdf))
    images = [MagicMock(), MagicMock()]
    convert = MagicMock(return_value=images)
    ocr = MagicMock(side_effect=["first page", "second page"])
    monkeypatch.setattr(text, "convert_from_path", convert)
    monkeypatch.setattr(text.pytesseract, "image_to_string", ocr)
    content, actual_method = text.extract_text(Path("invoice.pdf"))
    assert actual_method == method
    if method == "ocr":
        assert content == "first page\n\nsecond page"
        convert.assert_called_once_with(Path("invoice.pdf"), dpi=300)
        assert ocr.call_args_list == [call(image) for image in images]
        for image in images:
            image.close.assert_called_once()
    else:
        assert content == "\n\n".join(page_texts)
        convert.assert_not_called()


def test_ocr_failure_closes_every_image(monkeypatch):
    """Release rendered pages even if OCR fails on the first page."""
    pdf = MagicMock()
    pdf.__enter__.return_value.pages = []
    monkeypatch.setattr(text.pdfplumber, "open", MagicMock(return_value=pdf))
    images = [MagicMock(), MagicMock()]
    monkeypatch.setattr(text, "convert_from_path", MagicMock(return_value=images))
    monkeypatch.setattr(text.pytesseract, "image_to_string", MagicMock(side_effect=RuntimeError("OCR unavailable")))
    with pytest.raises(RuntimeError, match="OCR unavailable"):
        text.extract_text(Path("invoice.pdf"))
    for image in images:
        image.close.assert_called_once()


def test_native_multipage_invoice():
    """Read actual fixture contents from both pages in their original order."""
    path = Path(__file__).resolve().parents[1] / "data/invoices/inv_010.pdf"
    content, method = text.extract_text(path)
    assert method == "native"
    assert content.index("Process review") < content.index("Research summary")
    assert "23769.04" in content
