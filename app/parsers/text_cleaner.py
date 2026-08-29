"""
Cleans raw extracted SOW text before it is sent to the AI agent.

WHY: Extracted text from Word/PDF is messy — repeated headers/footers on
every page, stray page numbers, inconsistent blank lines. Feeding that noise
directly to Gemini wastes tokens and can confuse the model into treating a
footer as a requirement. This module normalizes everything into clean,
readable prose before it ever reaches the LLM layer.
"""

import re

from app.utils.logger import get_logger

logger = get_logger(__name__)

# Matches common page-number patterns: "Page 3", "3 of 10", "- 4 -", lone digits on a line.
_PAGE_NUMBER_PATTERNS = [
    re.compile(r"^\s*page\s+\d+(\s+of\s+\d+)?\s*$", re.IGNORECASE),
    re.compile(r"^\s*-?\s*\d+\s*-?\s*$"),
]

# Lines that are pure repeated boilerplate (very short, all-caps, or common footer words)
# are treated as probable headers/footers when they repeat across the document.
_BOILERPLATE_KEYWORDS = ("confidential", "proprietary", "all rights reserved", "draft copy")


def _is_page_number_line(line: str) -> bool:
    return any(pattern.match(line) for pattern in _PAGE_NUMBER_PATTERNS)


def _is_boilerplate_line(line: str) -> bool:
    lowered = line.strip().lower()
    return any(keyword in lowered for keyword in _BOILERPLATE_KEYWORDS)


def clean_text(raw_text: str) -> str:
    """Remove page numbers, boilerplate footers, extra blank lines, and normalize whitespace.

    Steps:
        1. Split into lines.
        2. Drop lines that are pure page numbers or known boilerplate.
        3. Collapse multiple blank lines into a single blank line.
        4. Normalize internal whitespace (tabs, multiple spaces) to single spaces.
        5. Strip leading/trailing whitespace from the whole document.
    """
    if not raw_text or not raw_text.strip():
        logger.warning("clean_text received empty input")
        return ""

    lines = raw_text.splitlines()
    kept_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        if not stripped:
            kept_lines.append("")  # preserve paragraph breaks, collapsed later
            continue

        if _is_page_number_line(stripped):
            continue

        if _is_boilerplate_line(stripped):
            continue

        # Normalize internal whitespace (tabs, repeated spaces)
        normalized = re.sub(r"[ \t]+", " ", stripped)
        kept_lines.append(normalized)

    text = "\n".join(kept_lines)

    # Collapse 3+ consecutive newlines down to exactly 2 (one blank line between paragraphs)
    text = re.sub(r"\n{3,}", "\n\n", text)

    cleaned = text.strip()
    logger.info(f"Cleaned text: {len(raw_text)} chars -> {len(cleaned)} chars")
    return cleaned
