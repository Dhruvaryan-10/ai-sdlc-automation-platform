"""
Shared helper for keeping a document's in-body '**Version:** N' line in sync
with the real tracked version number.

WHY THIS EXISTS (and lives here, not inside one agent):
Both the Business Analyst Agent (BRD) and the Solution Architect Agent (HLD)
ask their prompt templates to write a '**Version:** N' metadata line at the top
of the document. AI refinement is deliberately told to preserve unrelated
content unchanged, so that line would never get bumped on its own. Every
service therefore stamps the correct number in after each generation, edit, and
refinement rather than trusting the model to keep it in sync. Keeping the regex
and the stamping rule in one place means the two agents cannot drift apart.
"""

import re

from app.utils.logger import get_logger

logger = get_logger(__name__)

# Matches the "**Version:** N" metadata line our prompt templates produce.
_VERSION_LINE_PATTERN = re.compile(r"(\*\*Version:\*\*\s*)\d+")


def stamp_version_number(content: str, version_number: int) -> str:
    """Force the first in-document '**Version:** N' line to match the tracked version."""
    if _VERSION_LINE_PATTERN.search(content):
        return _VERSION_LINE_PATTERN.sub(rf"\g<1>{version_number}", content, count=1)
    logger.warning("Could not find a '**Version:**' line in document content to stamp")
    return content
