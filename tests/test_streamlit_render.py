"""
Tests for the display-only Markdown fixes in app/ui/streamlit_app.py
(`_hard_break_metadata_lines` + `render_artifact_markdown`).

These are pure-string / capture tests. No Gemini, no persistence writes.
Importing the UI module executes its top-level script once in bare mode (the
conftest sets a dummy GOOGLE_API_KEY and the autouse isolated_output_dir points
persistence at a tmp dir), which is deterministic and offline.
"""

import json
from pathlib import Path

from app.ui.streamlit_app import (
    _flatten_legacy_numbered_bullets,
    _hard_break_metadata_lines,
    render_artifact_markdown,
)

_METADATA_BLOCK = (
    "## TC-003 — Verify configuring multiple attributes\n"
    "\n"
    "**Requirement / User Story Reference:** FR-1.3\n"
    "**BRD Reference:** FR-1.3\n"
    "**User Story Reference:** US-003\n"
    "**HLD Reference:** Section 3.4\n"
    "**LLD Reference:** Section 10.1\n"
    "**Priority:** High\n"
    "**Test Type:** Business Rule\n"
    "**Dependencies:** TC-003\n"
    "\n"
    "**Preconditions:**\n"
    "- A base product exists\n"
)


# --- _hard_break_metadata_lines ------------------------------------------

def test_metadata_run_gets_hard_breaks_except_last_before_blank():
    out = _hard_break_metadata_lines(_METADATA_BLOCK).split("\n")

    # every metadata line followed by another metadata line ends with "  "
    assert out[2] == "**Requirement / User Story Reference:** FR-1.3  "
    assert out[3] == "**BRD Reference:** FR-1.3  "
    assert out[7] == "**Priority:** High  "
    assert out[8] == "**Test Type:** Business Rule  "
    # last metadata line in the run is followed by a blank line -> no trailing spaces
    assert out[9] == "**Dependencies:** TC-003"


def test_header_block_metadata_lines_also_break():
    header = (
        "# Proj — Test Cases\n"
        "\n"
        "**Version:** 1\n"
        "**Source:** Generated from artifacts\n"
        "**Built From:** BRD v1, HLD v1, LLD v1, User Stories v1\n"
        "**Client:** Pranav Corp\n"
        "**Project Type:** Web Application\n"
        "\n"
    )
    out = _hard_break_metadata_lines(header).split("\n")
    assert out[2] == "**Version:** 1  "
    assert out[3] == "**Source:** Generated from artifacts  "
    assert out[4].endswith("User Stories v1  ")
    assert out[5] == "**Client:** Pranav Corp  "
    assert out[6] == "**Project Type:** Web Application"  # last before blank


def test_label_only_lines_are_not_touched():
    src = "**Preconditions:**\n- item\n\n**Expected Result:**\nIt works\n\n**Notes:**\nA note\n"
    assert _hard_break_metadata_lines(src) == src


def test_ordinary_markdown_is_not_altered():
    src = (
        "# Heading\n"
        "## Sub — heading\n"
        "\n"
        "A normal paragraph line.\n"
        "Another prose line.\n"
        "\n"
        "- a bullet\n"
        "- **inline bold:** still a bullet\n"
        "1. a numbered step\n"
        "\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| **Key:** | in a table |\n"
    )
    assert _hard_break_metadata_lines(src) == src


def test_fenced_code_block_metadata_is_left_alone():
    src = (
        "**Real Key:** real value\n"
        "**Real Key 2:** real value\n"
        "\n"
        "```\n"
        "**Not Metadata:** inside a fence\n"
        "**Also Not:** inside a fence\n"
        "```\n"
    )
    out = _hard_break_metadata_lines(src).split("\n")
    assert out[0] == "**Real Key:** real value  "        # real metadata -> break
    assert out[4] == "**Not Metadata:** inside a fence"  # fenced -> untouched
    assert out[5] == "**Also Not:** inside a fence"


def test_transform_is_idempotent():
    once = _hard_break_metadata_lines(_METADATA_BLOCK)
    twice = _hard_break_metadata_lines(once)
    assert once == twice


def test_empty_and_none_inputs():
    assert _hard_break_metadata_lines("") == ""
    assert _hard_break_metadata_lines(None) == ""


def test_dollar_signs_are_not_escaped_by_the_break_transform():
    src = "**Price:** $29.99 and $19.99\n**Next:** x\n"
    out = _hard_break_metadata_lines(src)
    assert "$29.99 and $19.99" in out          # untouched by THIS transform
    assert out.startswith("**Price:** $29.99 and $19.99  ")


# --- _flatten_legacy_numbered_bullets --------------------------------

_LEGACY_STEPS = (
    "**Test Steps:**\n"
    "- 1. Navigate to the section.\n"
    "- 2. Click Add Product.\n"
    "- 3. Verify the product appears.\n"
    "\n"
    "**Expected Result:**\n"
)


def test_legacy_numbered_bullets_become_plain_ordered_list():
    out = _flatten_legacy_numbered_bullets(_LEGACY_STEPS).split("\n")
    assert out[1] == "1. Navigate to the section."
    assert out[2] == "2. Click Add Product."
    assert out[3] == "3. Verify the product appears."
    # label + blank + following label are untouched
    assert out[0] == "**Test Steps:**"
    assert out[4] == ""
    assert out[5] == "**Expected Result:**"


def test_legacy_paren_delimiter_is_normalised_to_dot():
    src = "- 1) Step one\n- 2) Step two\n"
    assert _flatten_legacy_numbered_bullets(src) == "1. Step one\n2. Step two\n"


def test_indented_legacy_numbered_bullet_keeps_indent():
    src = "  - 3. Nested-looking step\n"
    assert _flatten_legacy_numbered_bullets(src) == "  3. Nested-looking step\n"


def test_current_numbered_steps_are_unchanged():
    src = "**Test Steps:**\n1. Navigate to the section.\n2. Click Add Product.\n"
    assert _flatten_legacy_numbered_bullets(src) == src


def test_ordinary_bullets_are_unchanged_by_flatten():
    src = (
        "**Preconditions:**\n"
        "- A base product exists in the catalog.\n"
        "- User is logged in as an Administrator.\n"
        "\n"
        "**Test Data:**\n"
        "- Product ID: 1. not a step\n"      # "1." mid-text, not a leading marker
    )
    assert _flatten_legacy_numbered_bullets(src) == src


def test_flatten_does_not_touch_headings_prose_tables():
    src = (
        "# Heading\n"
        "## Sub — heading\n"
        "\n"
        "Prose mentioning - 1. inline is fine.\n"
        "\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| Steps | - 1. cell text |\n"
    )
    assert _flatten_legacy_numbered_bullets(src) == src


def test_flatten_skips_fenced_code_blocks():
    src = (
        "- 1. real legacy step\n"
        "\n"
        "```\n"
        "- 1. code sample line\n"
        "- 2) another code line\n"
        "```\n"
        "- 2. another real legacy step\n"
    )
    out = _flatten_legacy_numbered_bullets(src).split("\n")
    assert out[0] == "1. real legacy step"          # outside fence -> converted
    assert out[3] == "- 1. code sample line"        # inside fence -> untouched
    assert out[4] == "- 2) another code line"
    assert out[6] == "2. another real legacy step"


def test_flatten_is_idempotent():
    once = _flatten_legacy_numbered_bullets(_LEGACY_STEPS)
    assert _flatten_legacy_numbered_bullets(once) == once


def test_flatten_handles_empty_and_none():
    assert _flatten_legacy_numbered_bullets("") == ""
    assert _flatten_legacy_numbered_bullets(None) == ""


def test_flatten_does_not_escape_dollar_signs():
    src = "- 1. Pay $29.99 now\n- 2. Refund $19.99\n"
    assert _flatten_legacy_numbered_bullets(src) == "1. Pay $29.99 now\n2. Refund $19.99\n"


# --- render_artifact_markdown (composition) ---------------------------

def test_render_artifact_markdown_applies_both_transforms(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "app.ui.streamlit_app.st.markdown",
        lambda text, *a, **k: captured.setdefault("text", text),
    )
    render_artifact_markdown(
        "**Price:** $29.99 and B2B price $19.99\n**Priority:** High\n"
    )
    text = captured["text"]
    assert "\\$29.99" in text and "\\$19.99" in text          # $ escaping preserved
    assert text.startswith("**Price:** \\$29.99 and B2B price \\$19.99  ")  # hard break added


def test_render_pipeline_flattens_legacy_steps_then_keeps_other_fixes(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "app.ui.streamlit_app.st.markdown",
        lambda text, *a, **k: captured.setdefault("text", text),
    )
    render_artifact_markdown(
        "**Priority:** High\n"
        "**Test Type:** Business Rule\n"
        "\n"
        "**Test Steps:**\n"
        "- 1. Set price to $29.99\n"
        "- 2) Verify B2B price $19.99\n"
        "\n"
        "**Preconditions:**\n"
        "- A base product exists\n"
    )
    text = captured["text"]
    lines = text.split("\n")
    # legacy steps flattened to a plain ordered list, $ still escaped
    assert "1. Set price to \\$29.99" in lines
    assert "2. Verify B2B price \\$19.99" in lines
    assert "- 1. " not in text and "- 2) " not in text
    # metadata hard breaks still applied
    assert "**Priority:** High  " in lines
    # ordinary bullet still a bullet
    assert "- A base product exists" in lines


def test_render_artifact_markdown_handles_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "app.ui.streamlit_app.st.markdown",
        lambda text, *a, **k: captured.setdefault("text", text),
    )
    render_artifact_markdown(None)
    assert captured["text"] == ""


# --- persisted artifact is untouched ----------------------------------

def test_persisted_test_case_artifact_is_not_modified_by_preview():
    repo_root = Path(__file__).resolve().parents[1]
    versions_file = repo_root / "outputs" / "d1801c21" / "test_cases" / "versions.json"
    if not versions_file.exists():
        import pytest
        pytest.skip("no persisted d1801c21 project on this machine")

    before = versions_file.read_bytes()
    stored = json.loads(before.decode("utf-8"))[0]["content"]

    # Full preview pipeline (order matches render_artifact_markdown).
    transformed = _flatten_legacy_numbered_bullets(stored)
    transformed = _hard_break_metadata_lines(transformed)
    transformed = transformed.replace("$", "\\$")

    # the transform changed the *preview* string ...
    assert transformed != stored
    assert "**HLD Reference:** Section 3.4  " in transformed or "**Priority:** High  " in transformed
    # legacy "- N." steps are flattened for display
    assert "- 1. " not in transformed
    assert "\n1. " in transformed
    # ... but wrote nothing: the file on disk is byte-identical.
    assert versions_file.read_bytes() == before
    assert "- 1. " in stored  # the stored artifact still has the legacy shape
