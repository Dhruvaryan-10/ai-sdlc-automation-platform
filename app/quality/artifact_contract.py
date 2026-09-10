"""
Phase 17 - artifact structural contracts.

A PURE, stdlib-only quality-layer module. It answers one question for a freshly
generated artifact, BEFORE it is persisted as a version:

    "Does this document still parse the way every downstream consumer
     (traceability matrix, Project Quality Report, Closure Report) assumes?"

The ONLY parsing source is ``app.quality.traceability.extract_*`` - this module
never re-implements ID extraction and never adds a regex. It never mutates its
input and never repairs generated content; it only classifies.

Two severities:

  * ``blocking`` - a downstream-fatal shape violation. There is exactly one per
    artifact and it is always "nothing parsed at all": a BRD with zero
    FR-/NFR-/BR- identifiers, User Stories with zero ``US-`` headings, Test Cases
    with zero ``TC-`` identifiers. A generation service turns these into an
    ``ArtifactContractError`` (a ``ValueError`` subclass) via :func:`enforce`,
    which the Phase 12B error taxonomy classifies as ``state.invalid`` - the
    user sees a safe "try regenerating" message; the raw document never leaves.

  * ``warning`` - a real, evidenced anomaly that still lets the artifact through:
    e.g. requirements parsed but none carries a title, user stories with no
    ``**BRD Reference:**`` line, test cases with no reference fields. Services
    log these (app-authored strings only); they never block.

HLD and LLD: the current codebase demonstrates **no** structural invariant that
the HLD or LLD *document itself* must satisfy for downstream extraction (the
traceability matrix deliberately carries no requirement-level HLD/LLD mapping;
grounding checks are substring-only and tolerate any shape). :func:`check_hld` /
:func:`check_lld` therefore exist as documented no-ops - a place to add a real
invariant when one is introduced - and the HLD/LLD services are intentionally
left unchanged this phase.

``ArtifactContractError``'s message is assembled from app-authored strings only -
it never embeds the artifact text, the model output, a prompt, or a secret.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.quality.traceability import (
    extract_brd_requirements,
    extract_test_cases,
    extract_user_stories,
)

# --- artifact ids ---------------------------------------------------------
ARTIFACT_BRD = "brd"
ARTIFACT_USER_STORIES = "user_stories"
ARTIFACT_TEST_CASES = "test_cases"
ARTIFACT_HLD = "hld"
ARTIFACT_LLD = "lld"

# --- severities ---------------------------------------------------------
SEVERITY_BLOCKING = "blocking"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class ContractIssue:
    """One contract finding. ``message`` is always an app-authored, bounded,
    content-free string safe to show a user or write to a log."""

    code: str            # stable slug, e.g. "brd.no_requirements"
    severity: str         # SEVERITY_BLOCKING | SEVERITY_WARNING
    message: str


@dataclass(frozen=True)
class ContractReport:
    """The outcome of checking one artifact. Immutable; carries no artifact text."""

    artifact: str
    issues: tuple[ContractIssue, ...] = ()

    @property
    def blocking(self) -> tuple[ContractIssue, ...]:
        return tuple(i for i in self.issues if i.severity == SEVERITY_BLOCKING)

    @property
    def warnings(self) -> tuple[ContractIssue, ...]:
        return tuple(i for i in self.issues if i.severity == SEVERITY_WARNING)

    @property
    def ok(self) -> bool:
        """True when nothing downstream-fatal was found (warnings are still ok)."""
        return not self.blocking

    def warning_messages(self) -> list[str]:
        return [i.message for i in self.warnings]


class ArtifactContractError(ValueError):
    """Raised when a generated artifact fails its BLOCKING structural contract.

    The message is built only from :class:`ContractIssue` messages (app-authored,
    content-free). The originating :class:`ContractReport` is attached as
    ``.report`` for callers/tests; it too holds no artifact text.
    """

    def __init__(self, report: ContractReport) -> None:
        self.report = report
        reasons = "; ".join(i.message for i in report.blocking) or "unknown reason"
        super().__init__(
            f"The generated {report.artifact.replace('_', ' ')} did not meet a "
            f"required structural contract: {reasons}"
        )


# --- internal helpers ---------------------------------------------------

def _issue(code: str, severity: str, message: str) -> ContractIssue:
    return ContractIssue(code=code, severity=severity, message=message)


# --- per-artifact contracts -------------------------------------------

def check_brd(brd_text: str | None) -> ContractReport:
    """Contract for a generated BRD. Blocking: zero parseable requirement ids."""
    requirements = extract_brd_requirements(brd_text)
    issues: list[ContractIssue] = []

    if not requirements:
        issues.append(_issue(
            "brd.no_requirements", SEVERITY_BLOCKING,
            "no requirement identifiers (FR-, NFR- or BR-) were found; the "
            "traceability matrix and coverage reports would be empty",
        ))
        return ContractReport(artifact=ARTIFACT_BRD, issues=tuple(issues))

    if all(r.title is None for r in requirements):
        issues.append(_issue(
            "brd.untitled_requirements", SEVERITY_WARNING,
            "requirement identifiers were found but none has a bold title line; "
            "downstream requirement titles will be blank",
        ))
    if len(requirements) == 1:
        issues.append(_issue(
            "brd.few_requirements", SEVERITY_WARNING,
            "only one requirement identifier was parsed from the BRD; downstream "
            "coverage reporting will be very coarse",
        ))

    return ContractReport(artifact=ARTIFACT_BRD, issues=tuple(issues))


def check_user_stories(
    user_stories_text: str | None, *, brd_text: str | None = None
) -> ContractReport:
    """Contract for generated User Stories. Blocking: zero parseable ``US-``
    headings. When ``brd_text`` is supplied, a dangling BRD reference (a story
    citing a requirement id absent from the BRD) is reported as a warning."""
    stories = extract_user_stories(user_stories_text)
    issues: list[ContractIssue] = []

    if not stories:
        issues.append(_issue(
            "user_stories.no_headings", SEVERITY_BLOCKING,
            "no '## US-...' story headings were found; requirement-to-story "
            "coverage would be empty",
        ))
        return ContractReport(artifact=ARTIFACT_USER_STORIES, issues=tuple(issues))

    with_refs = [s for s in stories if s.brd_references]
    if not with_refs:
        issues.append(_issue(
            "user_stories.no_brd_references", SEVERITY_WARNING,
            "no story carries a '**BRD Reference:**' line; every requirement "
            "will look uncovered by user stories",
        ))
    elif len(with_refs) < len(stories):
        issues.append(_issue(
            "user_stories.partial_brd_references", SEVERITY_WARNING,
            "some user stories have no '**BRD Reference:**' line; their "
            "requirement links will be missing from the traceability matrix",
        ))

    if all(s.title is None for s in stories):
        issues.append(_issue(
            "user_stories.untitled", SEVERITY_WARNING,
            "no user story has a title after its '## US-...' heading",
        ))

    if brd_text is not None:
        known = {r.id for r in extract_brd_requirements(brd_text)}
        if known:
            dangling = sum(
                1 for s in stories for ref in s.brd_references if ref not in known
            )
            if dangling:
                issues.append(_issue(
                    "user_stories.dangling_brd_references", SEVERITY_WARNING,
                    "some user story BRD references point to requirement "
                    "identifiers that are not present in the BRD "
                    f"({dangling} reference(s))",
                ))

    return ContractReport(artifact=ARTIFACT_USER_STORIES, issues=tuple(issues))


def check_test_cases(test_cases_text: str | None) -> ContractReport:
    """Contract for generated Test Cases. Blocking: zero parseable ``TC-``
    identifiers."""
    cases = extract_test_cases(test_cases_text)
    issues: list[ContractIssue] = []

    if not cases:
        issues.append(_issue(
            "test_cases.no_identifiers", SEVERITY_BLOCKING,
            "no 'TC-NNN' identifiers were found; the test-coverage columns of "
            "every report would be empty",
        ))
        return ContractReport(artifact=ARTIFACT_TEST_CASES, issues=tuple(issues))

    def _has_any_ref(c) -> bool:
        return any((
            c.requirement_or_story_ref, c.brd_reference, c.user_story_reference,
            c.hld_reference, c.lld_reference,
        ))

    with_refs = [c for c in cases if _has_any_ref(c)]
    if not with_refs:
        issues.append(_issue(
            "test_cases.no_references", SEVERITY_WARNING,
            "no test case carries any requirement / story / design reference; "
            "the traceability matrix will show no test coverage",
        ))
    elif len(with_refs) < len(cases):
        issues.append(_issue(
            "test_cases.partial_references", SEVERITY_WARNING,
            "some test cases carry no reference field at all; their rows will "
            "not join to any requirement or story",
        ))

    return ContractReport(artifact=ARTIFACT_TEST_CASES, issues=tuple(issues))


def check_hld(hld_text: str | None) -> ContractReport:
    """No-op this phase - the codebase demonstrates no structural invariant the
    HLD *document* must satisfy for downstream extraction. Kept as an explicit
    extension point (see module docstring)."""
    return ContractReport(artifact=ARTIFACT_HLD, issues=())


def check_lld(lld_text: str | None) -> ContractReport:
    """No-op this phase - see :func:`check_hld`."""
    return ContractReport(artifact=ARTIFACT_LLD, issues=())


# --- enforcement -----------------------------------------------------

def enforce(report: ContractReport) -> ContractReport:
    """Raise :class:`ArtifactContractError` when ``report`` has any blocking
    issue; otherwise return the report unchanged (so callers can read
    ``.warnings`` afterwards)."""
    if report.blocking:
        raise ArtifactContractError(report)
    return report
