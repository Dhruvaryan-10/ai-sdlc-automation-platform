"""Extracts raw text from .pdf SOW files using PyMuPDF (imported as `fitz`)."""

from pathlib import Path

import fitz  # PyMuPDF

from app.utils.logger import get_logger

logger = get_logger(__name__)


def extract_text_from_pdf(file_path: str | Path) -> str:
    """Extract text from every page of a PDF file, in page order.

    Raises:
        ValueError: if the file cannot be opened as a valid PDF.
    """
    try:
        doc = fitz.open(file_path)
    except Exception as exc:
        logger.error(f"Failed to open PDF file '{file_path}': {exc}")
        raise ValueError(f"Could not read PDF file: {exc}") from exc

    pages_text: list[str] = []
    try:
        for page in doc:
            pages_text.append(page.get_text())
    finally:
        doc.close()

    text = "\n".join(pages_text)
    logger.info(f"Extracted {len(text)} characters from PDF '{file_path}'")
    return text
