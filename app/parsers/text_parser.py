"""Extracts raw text from .txt SOW files."""

from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)


def extract_text_from_txt(file_path: str | Path) -> str:
    """Read a plain text file, trying utf-8 first and falling back to latin-1.

    Raises:
        ValueError: if the file cannot be read as text at all.
    """
    path = Path(file_path)
    for encoding in ("utf-8", "latin-1"):
        try:
            text = path.read_text(encoding=encoding)
            logger.info(f"Extracted {len(text)} characters from TXT '{file_path}' ({encoding})")
            return text
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            logger.error(f"Failed to read TXT file '{file_path}': {exc}")
            raise ValueError(f"Could not read TXT file: {exc}") from exc

    raise ValueError(f"Could not decode TXT file '{file_path}' with supported encodings")
