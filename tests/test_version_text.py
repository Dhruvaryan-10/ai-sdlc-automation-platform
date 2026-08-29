"""Unit tests for the shared version-line stamping helper."""

import logging

from app.services import version_text
from app.services.version_text import stamp_version_number


def test_replaces_existing_version_line():
    content = "# Doc\n\n**Version:** 1\n\nBody"
    assert "**Version:** 7" in stamp_version_number(content, 7)


def test_only_first_occurrence_is_stamped():
    content = "**Version:** 1\nmiddle\n**Version:** 1\n"
    out = stamp_version_number(content, 5)
    assert out.count("**Version:** 5") == 1
    assert out.count("**Version:** 1") == 1


def test_no_version_line_is_a_noop():
    content = "# Doc\n\nNo version marker here."
    assert stamp_version_number(content, 3) == content


def test_no_version_line_logs_a_warning():
    # The project logger sets propagate=False, so attach a probe directly.
    records = []

    class _Probe(logging.Handler):
        def emit(self, record):
            records.append(record)

    probe = _Probe()
    version_text.logger.addHandler(probe)
    try:
        stamp_version_number("no marker", 3)
    finally:
        version_text.logger.removeHandler(probe)

    assert any("Could not find a '**Version:**' line" in r.getMessage() for r in records)


def test_preserves_surrounding_whitespace():
    assert stamp_version_number("**Version:**   2", 4) == "**Version:**   4"
