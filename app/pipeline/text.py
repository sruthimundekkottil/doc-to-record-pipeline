"""Read invoice text, falling back to OCR for PDFs with little native text."""

from pathlib import Path

import pdfplumber
import pytesseract
from pdf2image import convert_from_path


def extract_text(pdf_path: Path) -> tuple[str, str]:
    """Return all page text and its method, using OCR below 50 characters."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    if sum(len(page) for page in pages) >= 50:
        return "\n\n".join(pages), "native"

    images = convert_from_path(pdf_path, dpi=300)
    try:
        return "\n\n".join(pytesseract.image_to_string(image) for image in images), "ocr"
    finally:
        for image in images:
            image.close()
