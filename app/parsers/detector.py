"""
Detects the type of an uploaded SOW file so the correct parser can be chosen.

WHY A SEPARATE MODULE: keeping detection logic isolated means adding a new
supported format later (e.g. .rtf) only requires touching this enum + one
new parser, not the service layer that calls it.
"""

from enum import Enum
from pathlib import Path


class DocumentType(str, Enum):
    DOCX = "docx"
    PDF = "pdf"
    TXT = "txt"
    UNSUPPORTED = "unsupported"


_EXTENSION_MAP = {
    ".docx": DocumentType.DOCX,
    ".pdf": DocumentType.PDF,
    ".txt": DocumentType.TXT,
}


def detect_document_type(file_path: str | Path) -> DocumentType:
    """Detect document type purely from file extension.

    Extension-based detection is intentionally simple for Phase 1. If needed
    later, this can be upgraded to magic-byte sniffing (e.g. via `python-magic`)
    for cases where extensions are missing or wrong.
    """
    suffix = Path(file_path).suffix.lower()
    return _EXTENSION_MAP.get(suffix, DocumentType.UNSUPPORTED)
