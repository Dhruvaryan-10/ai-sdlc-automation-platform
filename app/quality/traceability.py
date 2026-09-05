"""
Phase 9A — Traceability & Quality Report (backend only, read-only, deterministic).

WHY THIS EXISTS: BRD requirements (FR-N / NFR-N / BR-N), User Stories (US-NNN,
each optionally carrying a `**BRD Reference:**` line), and Test Cases (TC-NNN,
each optionally carrying `requirement_or_story_ref` / `brd_reference` /
`user_story_reference` / `hld_reference` / `lld_reference`) already encode a
real BRD -> User Stories -> Test Cases traceability chain inside the Markdown
documents `VersionService` already persists. Nobody ever parses that chain back
out into a usable report. This module does exactly that, and only that.

HARD RULES (do not violate when extending this module):
  * Read-only. Never calls `add_version` / `mark_final` / `unlock_final` / any
    `generate_*` / `refine_*` method. Never touches `VersionService` for
    anything but the read-only getters every other service already uses.
  * Never calls Gemini (no agent is ever constructed or invoked here).
  * The authoritative req -> story -> test matrix is BRD -> User Stories ->
    Test Cases ONLY. HLD / LLD content is read ONLY to ground `hld_reference`
    / `lld_reference` values in the reference-grounding check below - this
    module never fabricates a requirement mapping for HLD or LLD, because
    neither artifact persists one (see the Phase 9 reconnaissance report).
  * `is_reference_grounded()` below duplicates the small comparison
    `TestCaseService._reference_is_grounded` already does (substring / bare
    leading-word match) rather than importing that private method, exactly
    the same "small deliberate duplication over cross-module coupling to a
    private method" convention this codebase already uses for `_extract_text`
    / `_invoke` across every agent. `TestCaseService._reference_is_grounded`
    itself is NOT modified, imported, or called from here.
  * Markdown formatting is NOT assumed to be perfectly uniform (real Gemini
    output varies: flat `FR-1` vs. grouped-decimal `FR-1.1`; bold vs. plain).
    Every extractor below is written to tolerate that.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.initial_user_story.service import InitialUserStoryService
    from app.agents.low_level_design.service import LowLevelDesignService
    from app.agents.solution_architect.service import SolutionArchitectService
    from app.agents.test_case.service import TestCaseService
    from app.services.version_service import BRDVersion

# --- shared ID patterns -----------------------------------------------------

# Bare requirement token, anywhere in text: FR-1, FR-1.2, NFR-3, BR-1.4.2, ...
_REQ_ID_TOKEN = r"(?:FR|NFR|BR)-\d+(?:\.\d+)*"
_REQ_ID_RE = re.compile(rf"\b({_REQ_ID_TOKEN})\b")

# A bold, optionally-titled requirement definition, e.g.
# "**FR-1.1: Product CRUD Operations**" or "**FR-1**" (title may be absent).
_REQ_TITLED_RE = re.compile(
    rf"\*\*(?P<id>{_REQ_ID_TOKEN})\s*:?\s*(?P<title>[^*\n]*?)\s*\*\*"
)

_US_HEADING_RE = re.compile(r"^##\s+(US-\d+)\s*(?:[—\-–]+\s*(.*))?$", re.MULTILINE)
_TC_HEADING_RE = re.compile(r"^##\s+(TC-\d{3})\s*(?:[—\-–]+\s*(.*))?$", re.MULTILINE)

_BRD_REFERENCE_LINE_RE = re.compile(r"^\*\*BRD Reference:\*\*\s*(.+)$", re.MULTILINE)

# Top-level artifact section heading, e.g. "## 8. Validation Rules".
_TOP_LEVEL_SECTION_RE = re.compile(r"^##\s+(\d+)\.\s+(.+)$", re.MULTILINE)

# The exact label lines `TestCaseService._render_markdown` writes (unchanged
# by this module - just read back out).
_TC_FIELD_LABELS = {
    "requirement_or_story_ref": "Requirement / User Story Reference",
    "brd_reference": "BRD Reference",
    "user_story_reference": "User Story Reference",
    "hld_reference": "HLD Reference",
    "lld_reference": "LLD Reference",
}


# --- pure data structures -----------------------------------------------

@dataclass(frozen=True)
class Requirement:
    """One BRD requirement, e.g. id="FR-1.2", kind="FR", title="Product Variants"."""

    id: str
    kind: str                 # "FR" | "NFR" | "BR"
    title: str | None = None


@dataclass(frozen=True)
class UserStoryRecord:
    """One User Story, e.g. id="US-002", brd_references=["FR-2.1", "BR-1"]."""

    id: str
    title: str | None
    brd_references: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TestCaseRecord:
    """One Test Case's reference fields, read back from the persisted document."""

    __test__ = False  # not a pytest test class despite the "Test" prefix

    id: str
    requirement_or_story_ref: str | None
    brd_reference: str | None
    user_story_reference: str | None
    hld_reference: str | None
    lld_reference: str | None


@dataclass(frozen=True)
class TraceabilityMatrixRow:
    """One BRD requirement's end-to-end trace, Phase 9A-2.

    `test_case_ids` is the stable-order, deduplicated UNION of
    `test_case_ids_direct` (a test case citing this requirement directly via
    `brd_reference`/`requirement_or_story_ref`) and `test_case_ids_via_story`
    (a test case reached only through one of this requirement's user stories,
    via `user_story_reference`). Phase 9A's own `requirement_to_test_cases()`
    only ever captures the direct set - real persisted project data shows a
    meaningful fraction of requirements (in one inspected project, 8 of 35)
    have test coverage that is ONLY visible through the story-mediated path,
    so a matrix row that only looked at the direct set would under-report
    real coverage. HLD/LLD are deliberately absent: no artifact persists a
    requirement-level HLD/LLD mapping (see the Phase 9/9A-2 reconnaissance
    reports), so none is fabricated here.
    """

    requirement_id: str
    requirement_kind: str          # "FR" | "NFR" | "BR"
    requirement_title: str | None
    user_story_ids: list[str]
    test_case_ids: list[str]
    test_case_ids_direct: list[str]
    test_case_ids_via_story: list[str]
    has_user_stories: bool
    has_test_cases: bool
    is_covered: bool                # has_user_stories and has_test_cases - objective, not a score


# --- 1. pure extraction functions ---------------------------------------

def extract_brd_requirements(brd_text: str | None) -> list[Requirement]:
    """Extract every FR-N / FR-N.M / NFR-N / BR-N requirement id from a BRD.

    Tolerates both flat ("FR-1") and grouped-decimal ("FR-1.1") ids, and both
    bold-titled definitions ("**FR-1.1: Title**") and bare mentions ("FR-1").
    Returns one `Requirement` per unique id, in first-seen order; the title
    (if any) comes from the first bold-titled occurrence of that id.
    """
    if not brd_text:
        return []

    titles: dict[str, str | None] = {}
    order: list[str] = []

    def _note(req_id: str, title: str | None) -> None:
        if req_id not in titles:
            titles[req_id] = title or None
            order.append(req_id)
        elif title and not titles[req_id]:
            titles[req_id] = title

    for m in _REQ_TITLED_RE.finditer(brd_text):
        _note(m.group("id"), m.group("title").strip() or None)
    for m in _REQ_ID_RE.finditer(brd_text):
        _note(m.group(1), None)

    return [Requirement(id=rid, kind=rid.split("-", 1)[0], title=titles[rid]) for rid in order]


def _iter_blocks(text: str, heading_re: re.Pattern[str]) -> list[tuple[re.Match[str], str]]:
    """Split `text` into (heading_match, block_text) pairs at each `heading_re` match.

    `block_text` runs from just after one heading LINE to just before the next
    (or end of document) - i.e. everything belonging to that item. The full
    `heading_match` (not just group(1)) is returned so callers can also read
    any title captured by the heading pattern itself.
    """
    matches = list(heading_re.finditer(text))
    blocks: list[tuple[re.Match[str], str]] = []
    for i, m in enumerate(matches):
        # Skip the newline right after the heading line, if present.
        start = m.end() + 1 if m.end() < len(text) and text[m.end()] == "\n" else m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append((m, text[start:end]))
    return blocks


def extract_user_stories(user_stories_text: str | None) -> list[UserStoryRecord]:
    """Extract every US-NNN id + its `**BRD Reference:**` values, if present.

    A story's title (the text after "## US-NNN") is best-effort only; the
    BRD-reference values are recovered by scanning that story's block for any
    FR-/NFR-/BR- token on its "**BRD Reference:**" line, regardless of how the
    model separated multiple ids (comma, "and", semicolon, ...).
    """
    if not user_stories_text:
        return []

    stories: list[UserStoryRecord] = []
    for heading, block in _iter_blocks(user_stories_text, _US_HEADING_RE):
        us_id = heading.group(1)
        title = (heading.group(2) or "").strip() or None

        refs: list[str] = []
        ref_line = _BRD_REFERENCE_LINE_RE.search(block)
        if ref_line:
            for tok in _REQ_ID_RE.findall(ref_line.group(1)):
                if tok not in refs:
                    refs.append(tok)

        stories.append(UserStoryRecord(id=us_id, title=title, brd_references=refs))

    return stories


def extract_test_cases(test_cases_text: str | None) -> list[TestCaseRecord]:
    """Extract every TC-NNN id + its five reference fields, if present.

    Reads back exactly the label lines `TestCaseService._render_markdown`
    writes ("Requirement / User Story Reference", "BRD Reference", "User
    Story Reference", "HLD Reference", "LLD Reference"); a field is None when
    its line is absent (the renderer omits falsy values entirely).
    """
    if not test_cases_text:
        return []

    field_res = {
        field_name: re.compile(rf"^\*\*{re.escape(label)}:\*\*\s*(.+)$", re.MULTILINE)
        for field_name, label in _TC_FIELD_LABELS.items()
    }

    cases: list[TestCaseRecord] = []
    for heading, block in _iter_blocks(test_cases_text, _TC_HEADING_RE):
        tc_id = heading.group(1)
        values: dict[str, str | None] = {}
        for field_name, pattern in field_res.items():
            m = pattern.search(block)
            values[field_name] = m.group(1).strip() if m else None
        cases.append(TestCaseRecord(id=tc_id, **values))

    return cases


# --- 2. traceability matrix (authoritative chain: BRD -> US -> TC) --------

def requirement_to_stories(
    requirements: list[Requirement], stories: list[UserStoryRecord]
) -> dict[str, list[str]]:
    """requirement id -> list of US ids whose BRD Reference cites it, in order."""
    mapping: dict[str, list[str]] = {r.id: [] for r in requirements}
    for story in stories:
        for ref in story.brd_references:
            if ref in mapping and story.id not in mapping[ref]:
                mapping[ref].append(story.id)
    return mapping


def story_to_test_cases(
    stories: list[UserStoryRecord], test_cases: list[TestCaseRecord]
) -> dict[str, list[str]]:
    """US id -> list of TC ids whose user_story_reference cites it, in order."""
    mapping: dict[str, list[str]] = {s.id: [] for s in stories}
    for tc in test_cases:
        ref = tc.user_story_reference
        if ref and ref in mapping and tc.id not in mapping[ref]:
            mapping[ref].append(tc.id)
    return mapping


def requirement_to_test_cases(
    requirements: list[Requirement], test_cases: list[TestCaseRecord]
) -> dict[str, list[str]]:
    """requirement id -> list of TC ids that cite it via brd_reference or
    requirement_or_story_ref (a test case may cite a requirement directly when
    no user story exists for it)."""
    mapping: dict[str, list[str]] = {r.id: [] for r in requirements}
    for tc in test_cases:
        for ref in (tc.brd_reference, tc.requirement_or_story_ref):
            if ref and ref in mapping and tc.id not in mapping[ref]:
                mapping[ref].append(tc.id)
    return mapping


def _coverage(mapping: dict[str, list[str]]) -> dict:
    total = len(mapping)
    covered_ids = [k for k, v in mapping.items() if v]
    uncovered_ids = [k for k, v in mapping.items() if not v]
    covered = len(covered_ids)
    return {
        "total": total,
        "covered": covered,
        "coverage_pct": round((covered / total) * 100, 1) if total else 0.0,
        "uncovered_ids": uncovered_ids,
    }


# --- 2b. Phase 9A-2: one matrix row per requirement, uniting the direct and
#         story-mediated test-case paths ------------------------------------

def build_traceability_matrix(
    requirements: list[Requirement],
    stories: list[UserStoryRecord],
    test_cases: list[TestCaseRecord],
) -> list[TraceabilityMatrixRow]:
    """Exactly one `TraceabilityMatrixRow` per requirement, in requirement order.

    Reuses `requirement_to_stories()` / `requirement_to_test_cases()` /
    `story_to_test_cases()` as-is - no parsing or matching logic is
    duplicated. `test_case_ids` is the stable-order deduplicated union of the
    direct set (from `requirement_to_test_cases()`) and the story-mediated set
    (this requirement's stories, per `requirement_to_stories()`, followed
    through `story_to_test_cases()`), direct ids first. HLD/LLD are never
    consulted or fabricated here - the matrix is authoritative only for
    BRD -> User Stories -> Test Cases.
    """
    req_to_story_ids = requirement_to_stories(requirements, stories)
    req_to_tc_direct = requirement_to_test_cases(requirements, test_cases)
    story_to_tc_ids = story_to_test_cases(stories, test_cases)

    rows: list[TraceabilityMatrixRow] = []
    for req in requirements:
        story_ids = req_to_story_ids.get(req.id, [])
        direct = req_to_tc_direct.get(req.id, [])

        via_story: list[str] = []
        for story_id in story_ids:
            for tc_id in story_to_tc_ids.get(story_id, []):
                if tc_id not in via_story:
                    via_story.append(tc_id)

        union: list[str] = []
        for tc_id in direct + via_story:
            if tc_id not in union:
                union.append(tc_id)

        has_stories = bool(story_ids)
        has_tests = bool(union)
        rows.append(TraceabilityMatrixRow(
            requirement_id=req.id,
            requirement_kind=req.kind,
            requirement_title=req.title,
            user_story_ids=story_ids,
            test_case_ids=union,
            test_case_ids_direct=direct,
            test_case_ids_via_story=via_story,
            has_user_stories=has_stories,
            has_test_cases=has_tests,
            is_covered=has_stories and has_tests,
        ))
    return rows


# The three requirement kinds this codebase's prompts/extractors ever
# produce (see `extract_brd_requirements`) - always present in `by_kind`,
# even with zero requirements of that kind, per the Phase 9A-2 contract.
_KNOWN_REQUIREMENT_KINDS = ("FR", "NFR", "BR")


def _matrix_coverage(rows: list[TraceabilityMatrixRow]) -> dict:
    total = len(rows)
    covered = sum(1 for r in rows if r.is_covered)
    uncovered_ids = [r.requirement_id for r in rows if not r.is_covered]
    return {
        "total": total,
        "covered": covered,
        "coverage_pct": round((covered / total) * 100, 1) if total else 0.0,
        "uncovered_ids": uncovered_ids,
    }


def summarize_traceability_matrix(rows: list[TraceabilityMatrixRow]) -> dict:
    """Aggregate + per-kind coverage over `rows`. Purely descriptive: each
    number is a direct count/percentage over objectively-measured `is_covered`
    flags - no weighting, no composite quality score.

    `by_kind` always includes "FR" / "NFR" / "BR" (with zeroed-out coverage
    for a kind absent from `rows`), because real persisted project data shows
    NFR story-coverage is structurally different from FR/BR (in one inspected
    project, 0% vs. ~90-100%) - blending them into one aggregate number would
    mask that distinction rather than reveal it. Any kind this codebase's
    extractors don't currently produce would still be included, in
    first-seen order, after the three known kinds.
    """
    summary = _matrix_coverage(rows)

    seen_kinds: list[str] = []
    for r in rows:
        if r.requirement_kind not in seen_kinds:
            seen_kinds.append(r.requirement_kind)
    ordered_kinds = list(_KNOWN_REQUIREMENT_KINDS) + [
        k for k in seen_kinds if k not in _KNOWN_REQUIREMENT_KINDS
    ]

    summary["by_kind"] = {
        kind: _matrix_coverage([r for r in rows if r.requirement_kind == kind])
        for kind in ordered_kinds
    }
    return summary


def find_orphan_references(
    requirements: list[Requirement],
    stories: list[UserStoryRecord],
    test_cases: list[TestCaseRecord],
) -> list[dict]:
    """Diagnostic only: references that LOOK like a BRD requirement id but do
    not match any id `extract_brd_requirements()` actually found.

    Checks `UserStoryRecord.brd_references`, `TestCaseRecord.brd_reference`,
    and `TestCaseRecord.requirement_or_story_ref` (only when that value is
    itself shaped like a requirement id - e.g. "FR-3" - and NOT a user-story
    id like "US-002", which `requirement_or_story_ref` may equally hold).
    Never changes any coverage/mapping computation and never attempts to
    repair or normalize the orphan value - purely reporting a broken link so
    a human can see it.
    """
    known_ids = {r.id for r in requirements}
    orphans: list[dict] = []

    for story in stories:
        for ref in story.brd_references:
            if ref not in known_ids:
                orphans.append({"source_id": story.id, "field": "brd_references", "value": ref})

    for tc in test_cases:
        if tc.brd_reference and tc.brd_reference not in known_ids:
            orphans.append({"source_id": tc.id, "field": "brd_reference", "value": tc.brd_reference})

        ref = tc.requirement_or_story_ref
        if ref and _REQ_ID_RE.fullmatch(ref.strip()) and ref.strip() not in known_ids:
            orphans.append({
                "source_id": tc.id, "field": "requirement_or_story_ref", "value": ref.strip(),
            })

    return orphans


# --- 3. improved reference grounding (reporting-only; does not touch
#        TestCaseService._reference_is_grounded) -----------------------

def is_reference_grounded(reference: str | None, document_text: str | None) -> bool:
    """True if `reference` is grounded in `document_text`.

    Reuses the conceptual behaviour of `TestCaseService._reference_is_grounded`
    (a `Section `/`Sec.`/`§` prefix is stripped, then the reference or its bare
    form is substring-matched against the document) - duplicated here rather
    than imported, per this codebase's established convention of small
    deliberate duplication over cross-module coupling to a private method.

    ADDITIONALLY recognizes a decimal sub-point citation such as
    "Section 8.2" / "Section 3.2.1" / "8.0" as grounded when the document has
    a real TOP-LEVEL heading "## 8. ..." / "## 3. ..." matching the
    reference's LEADING integer - even though the decimal suffix itself never
    appears literally (every BRD/HLD/LLD template here only ever emits flat,
    non-decimal top-level sections; a real Gemini QA agent nonetheless cites
    at decimal precision - see the Phase 9 reconnaissance report). A reference
    whose leading integer does NOT match any real top-level section is still
    correctly reported as NOT grounded.
    """
    if reference is None or not str(reference).strip():
        return True
    if not document_text:
        return False

    ref = str(reference).strip()
    bare = re.sub(r"^(section|sec\.?|§)\s+", "", ref, flags=re.IGNORECASE).strip()
    candidates = {ref, bare} if bare else {ref}

    lowered_doc = document_text.lower()
    if any(c.lower() in lowered_doc for c in candidates):
        return True

    leading = re.match(r"^(\d+)(?:\.\d+)*$", bare) if bare else None
    if leading:
        section_num = leading.group(1)
        if any(m.group(1) == section_num for m in _TOP_LEVEL_SECTION_RE.finditer(document_text)):
            return True

    return False


# --- 4. project-level report ---------------------------------------------

# Mirrors TestCaseService._check_traceability's own field -> source-artifact
# pairing exactly, so grounding checks stay consistent with the existing
# (unmodified) advisory check.
_GROUNDING_TARGETS = {
    "requirement_or_story_ref": ("brd", "user_stories"),
    "brd_reference": ("brd",),
    "user_story_reference": ("user_stories",),
    "hld_reference": ("hld",),
    "lld_reference": ("lld",),
}


def _select_version(service) -> tuple["BRDVersion | None", str | None]:
    """Final if present, else latest, else (None, None) - the SAME
    final-or-latest precedence `LowLevelDesignService`/`TestCaseService`
    already use for optional context, and the same "scan get_all_versions()
    for is_final" idiom `app/ui/streamlit_app.py` already uses. Deliberately
    goes through `get_all_versions()` ONLY - unlike `get_all_versions()`
    itself, each service names its own "get final" getter differently
    (`get_final_brd` / `get_final_hld` / `get_final_stories` / `get_final_lld`
    / `get_final`), so scanning the uniform list avoids depending on any of
    those five distinct names. Read-only; no writes."""
    versions = service.get_all_versions()
    if not versions:
        return None, None
    final = next((v for v in versions if v.is_final), None)
    if final is not None:
        return final, "final"
    return versions[-1], "latest"


def _artifact_summary(version, kind: str | None) -> dict:
    return {
        "exists": version is not None,
        "version_used": version.version if version is not None else None,
        "version_kind": kind,
    }


def build_project_traceability_report(
    project_id: str,
    *,
    ba_service: "BusinessAnalystService | None" = None,
    sa_service: "SolutionArchitectService | None" = None,
    us_service: "InitialUserStoryService | None" = None,
    lld_service: "LowLevelDesignService | None" = None,
    tc_service: "TestCaseService | None" = None,
) -> dict:
    """Build a deterministic traceability/quality report for `project_id`.

    Read-only: only `get_all_versions()` / `get_final_*()` getters are called
    on each service. Never generates, refines, finalizes, or persists
    anything, and never invokes Gemini. `*_service` are optional injection
    points (mirrors `sdlc_status()`'s own convention) - when omitted, plain
    real services are constructed for `project_id` (construction alone never
    calls Gemini; only `generate_*`/`refine_*` would, and none are called).

    HLD/LLD are read ONLY to ground `hld_reference`/`lld_reference` values -
    this report never fabricates a requirement mapping for either (the
    authoritative chain is BRD -> User Stories -> Test Cases; see the Phase 9
    reconnaissance report for why HLD/LLD have no persisted requirement
    mapping today).
    """
    # Local imports: keep this module importable without a network/Gemini
    # dependency at module-load time, and avoid a hard import-time coupling
    # for callers that only need the pure extraction functions above.
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.initial_user_story.service import InitialUserStoryService
    from app.agents.low_level_design.service import LowLevelDesignService
    from app.agents.solution_architect.service import SolutionArchitectService
    from app.agents.test_case.service import TestCaseService

    ba = ba_service or BusinessAnalystService(project_id=project_id)
    sa = sa_service or SolutionArchitectService(project_id=project_id, ba_service=ba)
    us = us_service or InitialUserStoryService(project_id=project_id, ba_service=ba)
    lld = lld_service or LowLevelDesignService(project_id=project_id, sa_service=sa, ba_service=ba)
    tc = tc_service or TestCaseService(project_id=project_id)

    brd_v, brd_kind = _select_version(ba)
    us_v, us_kind = _select_version(us)
    tc_v, tc_kind = _select_version(tc)
    hld_v, hld_kind = _select_version(sa)
    lld_v, lld_kind = _select_version(lld)

    brd_text = brd_v.content if brd_v else None
    us_text = us_v.content if us_v else None
    tc_text = tc_v.content if tc_v else None
    hld_text = hld_v.content if hld_v else None
    lld_text = lld_v.content if lld_v else None

    requirements = extract_brd_requirements(brd_text)
    stories = extract_user_stories(us_text)
    test_cases = extract_test_cases(tc_text)

    req_to_stories = requirement_to_stories(requirements, stories)
    story_to_tc = story_to_test_cases(stories, test_cases)
    req_to_tc = requirement_to_test_cases(requirements, test_cases)

    matrix_rows = build_traceability_matrix(requirements, stories, test_cases)
    matrix_summary = summarize_traceability_matrix(matrix_rows)
    orphan_references = find_orphan_references(requirements, stories, test_cases)

    texts_by_artifact = {
        "brd": brd_text, "user_stories": us_text, "hld": hld_text, "lld": lld_text,
    }

    reference_field_population: dict[str, dict] = {
        f: {"populated": 0, "total": len(test_cases)} for f in _TC_FIELD_LABELS
    }
    ungrounded_references: list[dict] = []

    for tc_case in test_cases:
        for field_name, artifact_keys in _GROUNDING_TARGETS.items():
            value = getattr(tc_case, field_name)
            if value in (None, ""):
                continue
            reference_field_population[field_name]["populated"] += 1

            target_texts = [texts_by_artifact[k] for k in artifact_keys]
            if not any(target_texts):
                continue  # that optional artifact wasn't supplied - nothing to check
            if not any(is_reference_grounded(value, t) for t in target_texts if t):
                ungrounded_references.append({
                    "test_case_id": tc_case.id,
                    "field": field_name,
                    "value": value,
                })

    return {
        "project_id": project_id,
        "brd": _artifact_summary(brd_v, brd_kind),
        "user_stories": _artifact_summary(us_v, us_kind),
        "test_cases": _artifact_summary(tc_v, tc_kind),
        "hld": _artifact_summary(hld_v, hld_kind),
        "lld": _artifact_summary(lld_v, lld_kind),
        "requirement_ids": [
            {"id": r.id, "kind": r.kind, "title": r.title} for r in requirements
        ],
        "user_story_ids": [
            {"id": s.id, "title": s.title} for s in stories
        ],
        "test_case_ids": [c.id for c in test_cases],
        "requirement_to_user_stories": req_to_stories,
        "user_story_to_test_cases": story_to_tc,
        "requirement_to_test_cases": req_to_tc,
        "requirement_to_story_coverage": _coverage(req_to_stories),
        "story_to_test_coverage": _coverage(story_to_tc),
        "reference_field_population": reference_field_population,
        "ungrounded_references": ungrounded_references,
        # --- Phase 9A-2: additive only - every key above is unchanged ---
        # Rows are converted to plain dicts (via `dataclasses.asdict`), matching
        # this report's existing convention (`requirement_ids`/`user_story_ids`
        # are already plain dicts, not raw dataclasses) and keeping the whole
        # report JSON-serializable, exactly like every other key here.
        "traceability_matrix": [asdict(row) for row in matrix_rows],
        "traceability_matrix_summary": matrix_summary,
        "orphan_references": orphan_references,
    }
