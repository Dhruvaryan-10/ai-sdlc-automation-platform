"""
Phase 9A-4 — Project Quality Report (backend only, read-only, deterministic).

WHY THIS EXISTS: Phase 9A / 9A-1 / 9A-2 already compute every objective fact a
"how complete/traceable is this project?" question needs - artifact existence
and version state, requirement/story/test-case extraction, the BRD -> User
Stories -> Test Cases traceability matrix and its coverage summary, reference
grounding, and orphan-reference detection - but spread across two functions
returning two differently-shaped results (`app.quality.traceability`'s report
and `app.orchestration.status.sdlc_status()`). This module aggregates the
existing facts into ONE project-quality answer. It adds NO new extraction, NO
new matching/grounding algorithm, and NO subjective score - every number here
is a direct count/percentage already computed by Phase 9A, or a trivial
groupby/subtraction over it.

HARD RULES (mirrors app/quality/traceability.py's own, unchanged):
  * Read-only. Never calls `add_version` / `mark_final` / `unlock_final` / any
    `generate_*` / `refine_*` method.
  * Never calls Gemini (no agent is ever INVOKED here; service construction
    alone - exactly like `build_project_traceability_report()` - never does).
  * No subjective composite "quality score". Every field is an objective
    count, percentage, or pass-through of already-computed Phase 9A data.
  * Composes ONLY the public API of `app.quality.traceability` - imports no
    private helper (`_coverage`, `_select_version`, `_artifact_summary`,
    `_GROUNDING_TARGETS`, `is_reference_grounded`) from that module, and does
    not import `app.orchestration.status` (no new quality -> orchestration
    dependency - see `_artifact_version_info()` below for why a small,
    trivial computation is duplicated here instead).
  * `app/quality/traceability.py` and its own tests are NOT modified by this
    module's existence - this file only ever calls that module's public
    functions/dataclasses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.quality.traceability import (
    Requirement,
    TestCaseRecord,
    UserStoryRecord,
    build_project_traceability_report,
    build_traceability_matrix,
    extract_brd_requirements,
    extract_test_cases,
    extract_user_stories,
    find_orphan_references,
    story_to_test_cases,
    summarize_traceability_matrix,
)

if TYPE_CHECKING:
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.initial_user_story.service import InitialUserStoryService
    from app.agents.low_level_design.service import LowLevelDesignService
    from app.agents.solution_architect.service import SolutionArchitectService
    from app.agents.test_case.service import TestCaseService
    from app.services.version_service import BRDVersion

# The one required TestCaseRecord field (enforced at ingestion by
# TestCaseService._REQUIRED_FIELDS / TestCaseModel) - the other four are
# optional context and must never be reported as a "failure" when absent.
_REQUIRED_TC_REFERENCE_FIELDS = {"requirement_or_story_ref"}

# Same five fields `app.quality.traceability`'s `_TC_FIELD_LABELS` / Phase 9A's
# `reference_field_population` already track, in the same order.
_TC_REFERENCE_FIELDS = (
    "requirement_or_story_ref",
    "brd_reference",
    "user_story_reference",
    "hld_reference",
    "lld_reference",
)


# --- 1. pure helpers (small, deliberately-duplicated trivial computations) --

def _story_to_test_coverage(
    stories: list[UserStoryRecord], test_cases: list[TestCaseRecord]
) -> dict:
    """{total, covered, coverage_pct, uncovered_ids} over `story_to_test_cases()`.

    "Stories with >=1 test case / total stories" is objectively justified here
    (unlike the requirement->test-case relationship, there is no intermediate
    entity between a story and a test case - a test case either cites the
    story directly via `user_story_reference` or it doesn't - so no
    story-mediation union is needed the way `build_traceability_matrix()`
    needed one for requirements). This duplicates the SAME trivial
    total/covered/uncovered tally `app.quality.traceability`'s private
    `_coverage()` already performs, rather than importing that private
    helper.
    """
    mapping = story_to_test_cases(stories, test_cases)
    total = len(mapping)
    covered_ids = [story_id for story_id, tc_ids in mapping.items() if tc_ids]
    uncovered_ids = [story_id for story_id, tc_ids in mapping.items() if not tc_ids]
    return {
        "total": total,
        "covered": len(covered_ids),
        "coverage_pct": round((len(covered_ids) / total) * 100, 1) if total else 0.0,
        "uncovered_ids": uncovered_ids,
    }


def _test_case_reference_population(test_cases: list[TestCaseRecord]) -> dict:
    """Per reference field: how many test cases populate it, and whether the
    schema actually requires it (only `requirement_or_story_ref` does - the
    other four are optional context; their absence is never a "failure")."""
    total = len(test_cases)
    return {
        field_name: {
            "populated": sum(1 for tc in test_cases if getattr(tc, field_name)),
            "total": total,
            "required": field_name in _REQUIRED_TC_REFERENCE_FIELDS,
        }
        for field_name in _TC_REFERENCE_FIELDS
    }


# --- 2. the pure report builder --------------------------------------------

def build_project_quality_report(
    requirements: list[Requirement],
    stories: list[UserStoryRecord],
    test_cases: list[TestCaseRecord],
    artifact_status: dict,
    *,
    ungrounded_references: list[dict] | None = None,
) -> dict:
    """Build a deterministic, JSON-serializable Project Quality Report.

    PURE: no filesystem I/O, no service construction, no Gemini/network call,
    and no input is mutated. `requirements` / `stories` / `test_cases` are the
    SAME structured records `app.quality.traceability`'s extractors already
    produce; `artifact_status` is the `{"brd": {...}, "hld": {...},
    "user_stories": {...}, "lld": {...}, "test_cases": {...}}` shape this
    module's own `build_project_quality_report_for_project()` derives (see
    `_artifact_version_info()`), passed through UNCHANGED.

    `ungrounded_references` is accepted as ALREADY-COMPUTED data (a plain list
    of `{"test_case_id", "field", "value"}` dicts, the exact shape
    `build_project_traceability_report()["ungrounded_references"]` already
    returns) rather than being derived here, because grounding requires the
    raw BRD/HLD/LLD/User-Story TEXT that `requirements`/`stories`/
    `test_cases`/`artifact_status` deliberately do not carry - re-deriving it
    here would mean either accepting raw text (breaking this function's
    purity/signature) or re-implementing `is_reference_grounded()` a second
    time (explicitly disallowed - grounding logic is reused, never
    duplicated). Defaults to `[]` for a fully standalone, text-free call.

    Requirement coverage reuses `build_traceability_matrix()` +
    `summarize_traceability_matrix()` verbatim - this IS the end-to-end
    coverage definition (`TraceabilityMatrixRow.is_covered`: >=1 user story
    AND >=1 test case, direct-citation or story-mediated); no second
    definition is introduced. Orphan references are surfaced via
    `find_orphan_references()` as diagnostics only - they never feed back
    into any coverage number computed here (the underlying mapping functions
    already silently ignore an unmatched reference id, independent of orphan
    detection).
    """
    ungrounded = list(ungrounded_references) if ungrounded_references else []

    matrix_rows = build_traceability_matrix(requirements, stories, test_cases)
    requirement_coverage = summarize_traceability_matrix(matrix_rows)
    user_story_coverage = _story_to_test_coverage(stories, test_cases)
    reference_population = _test_case_reference_population(test_cases)
    orphans = find_orphan_references(requirements, stories, test_cases)

    return {
        "artifact_status": artifact_status,
        "requirement_coverage": requirement_coverage,
        "user_story_coverage": user_story_coverage,
        "test_case_reference_population": reference_population,
        "grounding_findings": {"total": len(ungrounded), "entries": ungrounded},
        "orphan_references": {"total": len(orphans), "entries": orphans},
    }


# --- 3. artifact status (project-level only - needs real version history) --

def _artifact_version_info(service) -> tuple[int | None, int | None, "BRDVersion | None"]:
    """(latest_version, final_version, selected_version) for one artifact.

    `selected_version` is final-preferred (else latest, else None) - the SAME
    precedence `build_project_traceability_report()` already uses for every
    artifact, so this module never analyzes a different version than the
    existing Phase 9A report would for the same project. `latest_version` and
    `final_version` are reported SEPARATELY (unlike `app.quality.
    traceability`'s private `_select_version()`/`_artifact_summary()`, which
    collapse them into one chosen version) so a newer draft after
    finalization is visible rather than silently hidden - the exact facts
    `app/orchestration/status.py::sdlc_status()` already exposes.

    Deliberately duplicates that same trivial "scan get_all_versions() for
    is_final, else use the newest" computation locally rather than importing
    a private helper from `traceability.py` or depending on
    `app.orchestration.status` (which would pull in unrelated pipeline
    next-step/approval-gate vocabulary that isn't a "quality" concept, and
    would create a new quality -> orchestration coupling that doesn't exist
    today). Read-only; no writes.
    """
    versions = service.get_all_versions()
    if not versions:
        return None, None, None
    latest_version = versions[-1].version
    final = next((v for v in versions if v.is_final), None)
    final_version = final.version if final is not None else None
    selected = final if final is not None else versions[-1]
    return latest_version, final_version, selected


# --- 4. project-level wrapper (the only I/O in this module) ----------------

def build_project_quality_report_for_project(
    project_id: str,
    *,
    ba_service: "BusinessAnalystService | None" = None,
    sa_service: "SolutionArchitectService | None" = None,
    us_service: "InitialUserStoryService | None" = None,
    lld_service: "LowLevelDesignService | None" = None,
    tc_service: "TestCaseService | None" = None,
) -> dict:
    """Build the Project Quality Report for a real, persisted project.

    Read-only: only `get_all_versions()` is called on each service, plus
    `build_project_traceability_report()` (itself read-only) to obtain
    `ungrounded_references` without re-implementing grounding. Never
    generates, refines, finalizes, or persists anything, and never invokes
    Gemini (agent construction alone - identical to
    `build_project_traceability_report()`'s own pattern - never calls it;
    only `generate_*`/`refine_*` would, and none are called). `*_service` are
    optional injection points (mirrors `build_project_traceability_report()`'s
    own convention) - when omitted, plain real services are constructed for
    `project_id`, sharing one `BusinessAnalystService` (and
    `SolutionArchitectService`) as their upstream source, exactly like every
    other Phase 9A/9A-2 entry point.

    The quality report consumes whatever is ALREADY persisted - it never
    generates a missing artifact.
    """
    # Local imports: keep this module importable without a network/Gemini
    # dependency at module-load time (matches `build_project_traceability_
    # report()`'s own established convention exactly).
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

    brd_latest, brd_final, brd_v = _artifact_version_info(ba)
    hld_latest, hld_final, hld_v = _artifact_version_info(sa)
    us_latest, _us_final_unused, us_v = _artifact_version_info(us)
    lld_latest, lld_final, lld_v = _artifact_version_info(lld)
    tc_latest, tc_final, tc_v = _artifact_version_info(tc)

    artifact_status = {
        "brd": {"exists": brd_latest is not None, "latest_version": brd_latest, "final_version": brd_final},
        "hld": {"exists": hld_latest is not None, "latest_version": hld_latest, "final_version": hld_final},
        "user_stories": {"exists": us_latest is not None, "latest_version": us_latest, "final_version": None},
        "lld": {"exists": lld_latest is not None, "latest_version": lld_latest, "final_version": lld_final},
        "test_cases": {"exists": tc_latest is not None, "latest_version": tc_latest, "final_version": tc_final},
    }

    requirements = extract_brd_requirements(brd_v.content if brd_v else None)
    stories = extract_user_stories(us_v.content if us_v else None)
    test_cases = extract_test_cases(tc_v.content if tc_v else None)

    # Reuse the EXISTING, complete Phase 9A traceability report ONLY for
    # ungrounded_references - never re-implementing `is_reference_grounded()`
    # / `TestCaseService._reference_is_grounded()` here. Passing the SAME
    # already-constructed services avoids building a second, redundant set of
    # (never-invoked) Gemini-backed agents.
    traceability_report = build_project_traceability_report(
        project_id,
        ba_service=ba, sa_service=sa, us_service=us, lld_service=lld, tc_service=tc,
    )

    return build_project_quality_report(
        requirements, stories, test_cases, artifact_status,
        ungrounded_references=traceability_report["ungrounded_references"],
    )
