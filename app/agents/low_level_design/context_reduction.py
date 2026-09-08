"""
Phase 11C — deterministic LLD generation-context reduction.

WHY: the LLD generation prompt historically embedded the FULL accepted BRD and
the FULL latest User Stories as supporting context (~9k input tokens for a
mid-size project). The Phase 11C confirmatory experiment showed that a
deterministic *digest* of the BRD requirements plus a deterministic *index* of
the user stories carries everything the LLD actually consumes — every FR / NFR /
BR id with its intent line, and every user-story id with its BRD references and a
one-line goal — at roughly a quarter of the input tokens, with no measured loss
of downstream Test Case coverage, traceability, or grounding.

Everything here is pure, deterministic and Gemini-free. Requirement / story id
extraction is delegated to the existing authoritative extractors in
``app.quality.traceability``; only the per-requirement intent line and the
per-story goal line — which those extractors do not expose — are recovered here
with a small line scan.

SAFETY FALLBACK: :func:`build_reduced_lld_context` compares the reduced context
size against the equivalent full text. If the reduced form is not actually
smaller (e.g. a tiny BRD, or a pathological digest), it returns the full BRD +
full User Stories unchanged, so the LLD is never generated from a *larger*
context than before. The decision is returned (and logged by the caller); it is
never persisted and never changes versioning, provenance, or artifact format.

This module imports no agent package and constructs no service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.quality.traceability import (
    _US_HEADING_RE,
    _iter_blocks,
    extract_brd_requirements,
    extract_user_stories,
)

_DIGEST_HEADER = (
    "BRD REQUIREMENTS DIGEST\n"
    "(A deterministic, non-LLM extract of every functional requirement (FR), "
    "non-functional requirement (NFR) and business rule (BR) in the accepted "
    "BRD, each with its definition/intent line. Authoritative for the "
    "requirements it lists.)"
)

_INDEX_HEADER = (
    "USER STORY INDEX\n"
    "(A deterministic, non-LLM extract: every user-story id, the BRD requirement "
    "id(s) it references, and a one-line goal. Context only — NOT authoritative "
    "and NOT a requirement.)"
)

_KIND_LABELS = (
    ("FR", "Functional Requirements"),
    ("NFR", "Non-Functional Requirements"),
    ("BR", "Business Rules"),
)

# A requirement's own definition line, e.g. "FR-1. The system shall ..." or
# "NFR-3: 99.5% availability" or "BR-2) Only agents may ...". The id itself is
# already known (from extract_brd_requirements); this only recovers its prose.
_REQ_LINE_RE = re.compile(
    r"^\s*(?P<id>(?:FR|NFR|BR)-\d+(?:\.\d+)*)\s*[.:)\-]?\s*(?P<text>.*\S)?\s*$"
)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_METADATA_LINE_RE = re.compile(r"^\s*\*\*[^*]+:\*\*")

_MAX_DEFINITION_CHARS = 400
_MAX_GOAL_CHARS = 220

_DIGEST_HAS_REQ_RE = re.compile(r"(?m)^- (?:FR|NFR|BR)-\d")
_INDEX_HAS_STORY_RE = re.compile(r"(?m)^- US-\d")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _requirement_definitions(brd_text: str) -> dict[str, str]:
    """id -> its definition line (with any wrapped continuation lines folded in).

    Only the prose is recovered here; the authoritative id/kind list comes from
    ``extract_brd_requirements``.
    """
    lines = brd_text.splitlines()
    definitions: dict[str, str] = {}
    i = 0
    while i < len(lines):
        m = _REQ_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        parts = [m.group("text") or ""]
        j = i + 1
        while (
            j < len(lines)
            and lines[j].strip()
            and not _REQ_LINE_RE.match(lines[j])
            and not _HEADING_RE.match(lines[j])
        ):
            parts.append(lines[j])
            j += 1
        definitions.setdefault(m.group("id"), _collapse(" ".join(parts)))
        i = j
    return definitions


def build_brd_requirements_digest(brd_text: str) -> str:
    """A compact, deterministic digest of every FR / NFR / BR in ``brd_text``.

    Every requirement id ``extract_brd_requirements`` finds is retained, grouped
    by kind, each with its definition line (the bold title when the BRD used
    ``**FR-1: Title**`` form, otherwise the requirement's own sentence). Returns
    ``""`` only when the BRD contains no recognisable requirement id.
    """
    requirements = extract_brd_requirements(brd_text)
    if not requirements:
        return ""

    definitions = _requirement_definitions(brd_text)
    by_kind: dict[str, list[str]] = {kind: [] for kind, _ in _KIND_LABELS}
    for req in requirements:
        definition = req.title or definitions.get(req.id) or "(no definition line found)"
        if len(definition) > _MAX_DEFINITION_CHARS:
            definition = definition[:_MAX_DEFINITION_CHARS].rstrip() + " …"
        by_kind.setdefault(req.kind, []).append(f"- {req.id}: {definition}")

    sections: list[str] = [_DIGEST_HEADER, ""]
    for kind, label in _KIND_LABELS:
        if by_kind.get(kind):
            sections.append(f"{label}:")
            sections.extend(by_kind[kind])
            sections.append("")
    # any unexpected kind, after the three known ones
    for kind, bullets in by_kind.items():
        if kind not in {k for k, _ in _KIND_LABELS} and bullets:
            sections.append(f"{kind}:")
            sections.extend(bullets)
            sections.append("")
    return "\n".join(sections).strip()


def _story_goal(block: str, heading_title: str | None) -> str:
    """One-line goal for a story: its heading title plus the first real body
    line (usually the "As a … I want …" sentence), best-effort."""
    body_line = ""
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _METADATA_LINE_RE.match(line):
            continue
        line = line.lstrip("-*").strip()
        if line:
            body_line = _collapse(line)
            break
    goal = " ".join(p for p in (heading_title, body_line) if p).strip()
    if len(goal) > _MAX_GOAL_CHARS:
        goal = goal[:_MAX_GOAL_CHARS].rstrip() + " …"
    return goal


def build_user_story_index(user_stories_text: str) -> str:
    """A compact, deterministic index of every user story in ``user_stories_text``.

    Every ``US-NNN`` id is retained, with the BRD requirement id(s) from its
    ``**BRD Reference:**`` line and a one-line goal. Returns ``""`` only when no
    ``US-NNN`` heading is present.
    """
    stories = extract_user_stories(user_stories_text)
    if not stories:
        return ""

    blocks = {heading.group(1): body for heading, body in _iter_blocks(user_stories_text, _US_HEADING_RE)}
    lines: list[str] = [_INDEX_HEADER, ""]
    for story in stories:
        refs = ", ".join(story.brd_references) if story.brd_references else "—"
        goal = _story_goal(blocks.get(story.id, ""), story.title)
        suffix = f" — {goal}" if goal else ""
        lines.append(f"- {story.id} (BRD: {refs}){suffix}")
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class ReducedLLDContext:
    """The BRD / User-Story context blocks to hand the LLD agent.

    ``reduced`` is ``True`` when the deterministic digest + index are being used,
    ``False`` when the size guard fell back to the full BRD + full User Stories.
    ``reason`` is a short human string for logging only — nothing here is
    persisted.
    """

    brd_block: str
    user_stories_block: str
    reduced: bool
    reason: str
    full_chars: int
    reduced_chars: int


def build_reduced_lld_context(
    brd_text: str,
    user_stories_text: str,
    *,
    no_brd_sentinel: str,
    no_user_stories_sentinel: str,
) -> ReducedLLDContext:
    """Decide the LLD supporting-context blocks: reduced digest+index, or full.

    Deterministic and Gemini-free. A sentinel ("(no accepted BRD available)" /
    "(no draft user stories available)") is passed straight through. The reduced
    form is used only when it is genuinely smaller than the equivalent full text
    *and* it actually captured requirements / stories; otherwise the full text is
    returned unchanged so the LLD is never built from a larger context.
    """
    full_chars = len(brd_text) + len(user_stories_text)

    brd_is_sentinel = not brd_text.strip() or brd_text.strip() == no_brd_sentinel.strip()
    us_is_sentinel = (
        not user_stories_text.strip()
        or user_stories_text.strip() == no_user_stories_sentinel.strip()
    )

    digest = "" if brd_is_sentinel else build_brd_requirements_digest(brd_text)
    index = "" if us_is_sentinel else build_user_story_index(user_stories_text)

    digest_ok = brd_is_sentinel or bool(_DIGEST_HAS_REQ_RE.search(digest))
    index_ok = us_is_sentinel or bool(_INDEX_HAS_STORY_RE.search(index))

    brd_block = brd_text if brd_is_sentinel else digest
    us_block = user_stories_text if us_is_sentinel else index
    reduced_chars = len(brd_block) + len(us_block)

    if not (digest_ok and index_ok):
        return ReducedLLDContext(
            brd_text, user_stories_text, False,
            "deterministic extract captured nothing — using full BRD + User Stories",
            full_chars, full_chars,
        )
    if reduced_chars >= full_chars:
        return ReducedLLDContext(
            brd_text, user_stories_text, False,
            "reduced context is not smaller — using full BRD + User Stories",
            full_chars, reduced_chars,
        )
    return ReducedLLDContext(
        brd_block, us_block, True,
        "using deterministic BRD Requirements Digest + User Story Index",
        full_chars, reduced_chars,
    )
