"""
Closure Report Service (Phase 7).

WHY: The single orchestration point for the PROJECT CLOSURE REPORT - the final,
evidence-based synthesis of the SDLC. It is NOT the Project Quality Report; it
CONSUMES that report (plus the Traceability report) as its primary evidence and
adds a deterministic closure assessment and a Gemini-written narrative on top.

    generate()    -> require FINAL BRD -> assemble deterministic evidence
                     -> decide closure status (deterministic) -> agent narrative
                     -> render Markdown -> append v1 to the OWN closure stream
    regenerate()  -> same, appended as the next version (never overwrites)
    choose_final / unlock_final / get_final  -> human finalization (separate)

HARD PREREQUISITE: only the absence of a FINAL BRD blocks generation
(`NoFinalBRDError`, local to this package - same shape as the other agents'
gate errors). EVERYTHING else that is missing or not finalized (no final HLD,
no final LLD, no user stories, no test cases, test cases not finalized,
uncovered requirements/stories, ungrounded/orphan references) is REPRESENTED IN
THE REPORT as an explicit finding and drives the closure status - it never
fails generation.

DETERMINISTIC vs AI:
  * This service calculates every fact: artifact existence / latest / final /
    finalization status, requirement & story & test-case counts, coverage,
    grounding & orphan findings, uncovered items, blockers, and the closure
    status itself. All of that comes from `app.quality.*` (reused verbatim) -
    no coverage/grounding/version logic is re-implemented here.
  * Gemini (via `ClosureReportAgent`) writes NARRATIVE PROSE ONLY and is
    explicitly forbidden (in the prompt) from computing anything or changing
    the closure status.

CLOSURE STATUS - deterministic, exactly three values (see `_decide_closure_status`):
  * NOT_READY_FOR_CLOSURE     - a required final artifact is missing: no final
                                BRD (cannot occur past the hard gate), no final
                                HLD, no final LLD, no user stories at all, no
                                test cases at all, or test cases not finalized.
  * CLOSURE_WITH_OPEN_ITEMS   - all required final evidence is present, but
                                objective non-blocking findings remain
                                (uncovered requirements, uncovered user stories,
                                ungrounded references, or orphan references).
  * READY_FOR_CLOSURE         - all required final evidence is present AND no
                                blocking objective finding remains.
  There is NO numeric quality score and no "production ready" claim.

STORAGE: closure reports live in their own append-only stream at
outputs/{project_id}/closure_report/versions.json via the shared VersionService
(subdir "closure_report"). The stored `content` is a Markdown document (same
convention as every other artifact). Generation NEVER finalizes; finalization is
a separate human action (`choose_final`).

INDEPENDENCE: this module imports no other agent package's implementation - only
the shared `ProjectMetadata` value type (from business_analyst) and this
package's own `agent`. All project evidence is read through `app.quality.*`
public functions and its own `VersionService`.
"""

import json
import re
from datetime import date

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.closure_report.agent import ClosureReportAgent
from app.quality.project_quality_report import build_project_quality_report_for_project
from app.quality.traceability import build_project_traceability_report
from app.services.version_service import BRDVersion, VersionService
from app.utils.logger import get_logger

logger = get_logger(__name__)

_UNAVAILABLE = "Not available from project evidence."

# Same BRD-header regexes the other multi-source services use to recover project
# metadata from the accepted BRD (deliberate per-service duplication).
_TITLE_PATTERN = re.compile(
    r"^#\s+(.+?)\s+[—\-–]\s+Business Requirement Document", re.MULTILINE
)
_CLIENT_PATTERN = re.compile(r"\*\*Client:\*\*\s*(.+)")
_PROJECT_TYPE_PATTERN = re.compile(r"\*\*Project Type:\*\*\s*(.+)")
_ANY_H1_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)

_NARRATIVE_FIELDS = (
    "executive_summary",
    "scope_summary",
    "findings_summary",
    "outstanding_items_summary",
    "closure_summary",
    "limitations",
)

# Closure Report source_ref is the composite
#   "brd_v{b};hld_v{h};lld_v{l};us_v{u};tc_v{t}"   (each token an int or "none")
# — the exact form written by `_format_source_ref`. This parses it back.
_REF_TOKEN_PATTERN = re.compile(r"(brd|hld|lld|us|tc)_v(\d+|none)")

# Closure status vocabulary - deterministic, code-owned (never model-chosen).
STATUS_READY = "READY_FOR_CLOSURE"
STATUS_OPEN_ITEMS = "CLOSURE_WITH_OPEN_ITEMS"
STATUS_NOT_READY = "NOT_READY_FOR_CLOSURE"

_READINESS_PHRASES = {
    STATUS_READY: "Ready for closure",
    STATUS_OPEN_ITEMS: "Conditionally ready - closure with open items",
    STATUS_NOT_READY: "Not ready for closure",
}

# One-line, plain-English gloss of each deterministic closure status, so a
# CLOSURE_WITH_OPEN_ITEMS with an empty blocker list is not confusing.
_STATUS_GLOSS = {
    STATUS_NOT_READY: (
        "one or more required finalized artifacts are missing, so the project is "
        "not ready to close."
    ),
    STATUS_OPEN_ITEMS: (
        "every required finalized artifact is present and no issue blocks "
        "closure, but one or more objective coverage or evidence gaps remain for "
        "human review."
    ),
    STATUS_READY: (
        "every required finalized artifact is present and no blocking issue or "
        "outstanding coverage gap remains."
    ),
}

_LIMITATIONS = [
    "Only the SDLC artifacts the platform has persisted were examined; artifacts "
    "that were never generated or never finalized are reported as such.",
    "It does not verify runtime behaviour, deployment, or implementation "
    "quality, and it does not assign a numeric quality score.",
    "Coverage and evidence gaps recorded here remain pending human review before "
    "the project is formally closed.",
]


class NoFinalBRDError(Exception):
    """Raised when closure-report generation is attempted without an accepted/final BRD.

    Local to this package (same shape as the other agents' gate errors).
    """


class ClosureReportLockedError(Exception):
    """Raised when generation is attempted while the final closure report is locked."""


class InvalidClosureNarrativeError(ValueError):
    """Raised when the agent's output is not the expected closure-narrative JSON."""


class ClosureReportService:
    """Orchestrates project evidence -> a versioned Closure Report for one project."""

    def __init__(self, project_id: str, agent: ClosureReportAgent | None = None):
        self.project_id = project_id
        self._agent = agent or ClosureReportAgent()
        # Required source (BRD text + project metadata).
        self._brd = VersionService(project_id=project_id)
        # Read-only views of the upstream streams — used ONLY for post-generation
        # staleness introspection (Phase 10B); never written here.
        self._hld = VersionService(project_id=project_id, subdir="hld")
        self._lld = VersionService(project_id=project_id, subdir="lld")
        self._us = VersionService(project_id=project_id, subdir="user_stories")
        self._tc = VersionService(project_id=project_id, subdir="test_cases")
        # Own stream (read + write).
        self._cr = VersionService(project_id=project_id, subdir="closure_report")

    # --- prerequisites ------------------------------------------------------

    def _require_final_brd(self) -> BRDVersion:
        final_brd = self._brd.get_final_version()
        if final_brd is None:
            raise NoFinalBRDError(
                "Accept a BRD before generating the project closure report."
            )
        return final_brd

    def _guard_unlocked(self) -> None:
        final = self._cr.get_final_version()
        if final and final.is_locked:
            raise ClosureReportLockedError(
                "The final closure report is locked. Unlock it before regenerating."
            )

    # --- deterministic evidence assembly (reads persistence; NO Gemini, NO writes) --

    def _assemble_evidence(self) -> dict:
        """Compose ONE deterministic, JSON-serializable evidence bundle.

        Reuses `app.quality.*` verbatim: `build_project_quality_report_for_project`
        (artifact status + coverage + grounding + orphan findings) and
        `build_project_traceability_report` (the requirement->story->test matrix).
        No count, percentage, grounding check, or version selection is
        re-implemented here - every figure is a pass-through of an
        already-computed Phase 9A value. Read-only.
        """
        # Called with `project_id` only: each builder constructs its own
        # read-only services internally (no Gemini, no writes). The small
        # redundancy keeps this service free of any other-agent import.
        quality = build_project_quality_report_for_project(self.project_id)
        traceability = build_project_traceability_report(self.project_id)

        art = quality["artifact_status"]
        req_cov = quality["requirement_coverage"]
        us_cov = quality["user_story_coverage"]
        grounding = quality["grounding_findings"]
        orphans = quality["orphan_references"]
        matrix = traceability["traceability_matrix"]
        test_case_ids = traceability["test_case_ids"]

        def _status_word(info: dict, *, has_final_stage: bool = True) -> str:
            if not info["exists"]:
                return "missing"
            if not has_final_stage:
                return "available (no finalization stage)"
            return "final" if info["final_version"] is not None else "draft only (not finalized)"

        artifact_status = {
            "sow": {
                "exists": False, "latest_version": None, "final_version": None,
                "status": "not tracked", "evidence": _UNAVAILABLE,
            },
            "brd": {**art["brd"], "status": _status_word(art["brd"]),
                    "final_stage": True},
            "hld": {**art["hld"], "status": _status_word(art["hld"]),
                    "final_stage": True},
            "user_stories": {**art["user_stories"],
                             "status": _status_word(art["user_stories"], has_final_stage=False),
                             "final_stage": False,
                             "note": "User Stories have no finalization stage in this architecture."},
            "lld": {**art["lld"], "status": _status_word(art["lld"]),
                    "final_stage": True},
            "test_cases": {**art["test_cases"], "status": _status_word(art["test_cases"]),
                           "final_stage": True},
        }

        version_selection = [self._version_selection_note(name, artifact_status[name])
                             for name in ("brd", "hld", "user_stories", "lld", "test_cases")]

        requirements_with_stories = sum(1 for r in matrix if r["has_user_stories"])
        requirements_with_test_cases = sum(1 for r in matrix if r["has_test_cases"])
        # Decompose the COVERED set (`is_covered` == >=1 user story AND >=1 test
        # case) by HOW each covered requirement's test-case evidence is reached.
        # A covered row's `test_case_ids` union is non-empty, so if its
        # `test_case_ids_direct` list is empty the evidence is purely
        # story-mediated. The two counts are therefore mutually exclusive and
        # sum to `covered_requirements` (Section 4). (The earlier version counted
        # `test_case_ids_direct` and `test_case_ids_via_story` over ALL rows,
        # which double-counted every requirement reached both ways and did not
        # reconcile with the covered total.)
        covered_rows = [r for r in matrix if r["is_covered"]]
        covered_with_direct_tc = sum(
            1 for r in covered_rows if r["test_case_ids_direct"]
        )
        covered_via_story_only = sum(
            1 for r in covered_rows if not r["test_case_ids_direct"]
        )

        req_pop = quality["test_case_reference_population"].get(
            "requirement_or_story_ref", {"populated": 0, "total": len(test_case_ids)}
        )

        missing_final_artifacts = self._missing_final_artifacts(artifact_status)

        evidence = {
            "project_id": self.project_id,
            "generated_on": date.today().isoformat(),
            "artifact_status": artifact_status,
            "version_selection": version_selection,
            "requirements_traceability": {
                "total_requirements": req_cov["total"],
                "requirements_with_user_stories": requirements_with_stories,
                "requirements_with_test_cases": requirements_with_test_cases,
                "covered_requirements": req_cov["covered"],
                "uncovered_requirements": list(req_cov["uncovered_ids"]),
                "coverage_pct": req_cov["coverage_pct"],
                "coverage_by_kind": req_cov.get("by_kind", {}),
            },
            "user_story_summary": {
                "total_user_stories": us_cov["total"],
                "stories_covered_by_test_cases": us_cov["covered"],
                "uncovered_user_stories": list(us_cov["uncovered_ids"]),
                "story_to_test_coverage_pct": us_cov["coverage_pct"],
            },
            "test_coverage_summary": {
                "total_test_cases": len(test_case_ids),
                "requirement_or_story_ref_populated": {
                    "populated": req_pop["populated"], "total": req_pop["total"],
                },
                "covered_requirements_with_direct_test_case": covered_with_direct_tc,
                "covered_requirements_via_user_story_only": covered_via_story_only,
            },
            "quality_findings": {
                "grounding_findings": {"total": grounding["total"],
                                       "entries": list(grounding["entries"])},
                "orphan_references": {"total": orphans["total"],
                                      "entries": list(orphans["entries"])},
                "uncovered_requirements": list(req_cov["uncovered_ids"]),
                "uncovered_user_stories": list(us_cov["uncovered_ids"]),
                "missing_final_artifacts": missing_final_artifacts,
            },
        }
        return evidence

    @staticmethod
    def _version_selection_note(name: str, info: dict) -> str:
        labels = {
            "brd": "BRD", "hld": "HLD", "user_stories": "User Stories",
            "lld": "LLD", "test_cases": "Test Cases",
        }
        label = labels[name]
        latest, final = info["latest_version"], info["final_version"]
        if not info["exists"]:
            return f"{label}: no version exists."
        if not info["final_stage"]:
            return f"{label}: using latest v{latest} (this artifact has no finalization stage)."
        if final is None:
            return (
                f"{label}: NOT finalized - evidence is the latest draft v{latest}; "
                f"no accepted version is available."
            )
        if latest != final:
            return (
                f"{label}: using accepted final v{final}; a newer draft v{latest} "
                f"exists and was NOT used as accepted evidence."
            )
        return f"{label}: using accepted final v{final} (latest is also v{final})."

    @staticmethod
    def _missing_final_artifacts(artifact_status: dict) -> list[str]:
        """Required artifacts that are absent or not finalized (deterministic)."""
        missing: list[str] = []
        labels = {"brd": "BRD", "hld": "HLD", "lld": "LLD", "test_cases": "Test Cases"}
        for key, label in labels.items():
            info = artifact_status[key]
            if not info["exists"] or info["final_version"] is None:
                missing.append(label)
        if not artifact_status["user_stories"]["exists"]:
            missing.append("User Stories")
        return missing

    # --- deterministic closure status (pure) ------------------------------

    @staticmethod
    def _decide_closure_status(evidence: dict) -> str:
        """Return exactly one of READY_FOR_CLOSURE / CLOSURE_WITH_OPEN_ITEMS /
        NOT_READY_FOR_CLOSURE from the deterministic evidence. Pure. Gemini
        never influences this.

        Rules (documented, tested in tests/test_closure_report_service.py):
          1. NOT_READY_FOR_CLOSURE if a required final artifact is missing:
             no final BRD, no final HLD, no final LLD, no user stories at all,
             no test cases at all, or test cases exist but are not finalized.
          2. else CLOSURE_WITH_OPEN_ITEMS if any objective non-blocking finding
             remains: uncovered requirements, uncovered user stories,
             ungrounded references, or orphan references.
          3. else READY_FOR_CLOSURE.
        """
        art = evidence["artifact_status"]
        missing_required = (
            art["brd"]["final_version"] is None
            or art["hld"]["final_version"] is None
            or art["lld"]["final_version"] is None
            or not art["user_stories"]["exists"]
            or not art["test_cases"]["exists"]
            or art["test_cases"]["final_version"] is None
        )
        if missing_required:
            return STATUS_NOT_READY

        qf = evidence["quality_findings"]
        open_items = bool(
            qf["uncovered_requirements"]
            or qf["uncovered_user_stories"]
            or qf["grounding_findings"]["total"]
            or qf["orphan_references"]["total"]
        )
        return STATUS_OPEN_ITEMS if open_items else STATUS_READY

    @staticmethod
    def _readiness_phrase(status: str) -> str:
        return _READINESS_PHRASES.get(status, "Unknown")

    @staticmethod
    def _blockers(evidence: dict, status: str) -> list[str]:
        """Artifact/finalization gaps that prevent closure. Non-empty only when
        the status is NOT_READY_FOR_CLOSURE. Deterministic."""
        if status != STATUS_NOT_READY:
            return []
        art = evidence["artifact_status"]
        out: list[str] = []

        def _artifact_blocker(key: str, label: str) -> None:
            info = art[key]
            if not info["exists"]:
                out.append(f"No {label} has been generated for the project.")
            elif info["final_version"] is None:
                out.append(
                    f"The {label} has not been finalized "
                    f"(latest draft is v{info['latest_version']})."
                )

        _artifact_blocker("brd", "Business Requirement Document")
        _artifact_blocker("hld", "High-Level Design")
        _artifact_blocker("lld", "Low-Level Design")
        if not art["user_stories"]["exists"]:
            out.append("No user stories have been generated for the project.")
        _artifact_blocker("test_cases", "Test Cases")
        return out

    @staticmethod
    def _outstanding_items(evidence: dict) -> list[str]:
        """Objective, evidence-supported non-blocking findings. Deterministic.

        These describe absence of *traceability / test-case evidence* only — the
        platform does not execute the application or its tests, so nothing here
        implies an item was tested and failed, nor that runtime / end-to-end
        testing occurred.
        """
        qf = evidence["quality_findings"]
        out: list[str] = []
        uncov_req = qf["uncovered_requirements"]
        if uncov_req:
            noun = "requirement" if len(uncov_req) == 1 else "requirements"
            verb = "has" if len(uncov_req) == 1 else "have"
            out.append(
                f"{len(uncov_req)} {noun} {verb} no test-case coverage by the "
                f"available traceability evidence: "
                f"{', '.join(map(str, uncov_req))}."
            )
        uncov_us = qf["uncovered_user_stories"]
        if uncov_us:
            noun = "user story" if len(uncov_us) == 1 else "user stories"
            verb = "has" if len(uncov_us) == 1 else "have"
            out.append(
                f"{len(uncov_us)} {noun} {verb} no linked test cases: "
                f"{', '.join(map(str, uncov_us))}."
            )
        if qf["grounding_findings"]["total"]:
            n = qf["grounding_findings"]["total"]
            noun = "reference" if n == 1 else "references"
            verb = "is" if n == 1 else "are"
            out.append(
                f"{n} test-case {noun} {verb} not grounded in the cited artifact."
            )
        if qf["orphan_references"]["total"]:
            n = qf["orphan_references"]["total"]
            noun = "reference" if n == 1 else "references"
            verb = "does" if n == 1 else "do"
            out.append(
                f"{n} orphan {noun} {verb} not match any known requirement or "
                f"user story."
            )
        return out

    @staticmethod
    def _evidence_summary(evidence: dict) -> dict:
        rt = evidence["requirements_traceability"]
        us = evidence["user_story_summary"]
        tc = evidence["test_coverage_summary"]
        qf = evidence["quality_findings"]
        art = evidence["artifact_status"]
        final_present = [
            label for key, label in
            (("brd", "BRD"), ("hld", "HLD"), ("lld", "LLD"), ("test_cases", "Test Cases"))
            if art[key]["exists"] and art[key]["final_version"] is not None
        ]
        return {
            "requirements_total": rt["total_requirements"],
            "requirements_covered": rt["covered_requirements"],
            "requirements_uncovered": len(rt["uncovered_requirements"]),
            "requirement_coverage_pct": rt["coverage_pct"],
            "user_stories_total": us["total_user_stories"],
            "user_stories_covered_by_tests": us["stories_covered_by_test_cases"],
            "test_cases_total": tc["total_test_cases"],
            "ungrounded_references": qf["grounding_findings"]["total"],
            "orphan_references": qf["orphan_references"]["total"],
            "final_artifacts_present": final_present,
            "final_artifacts_missing": qf["missing_final_artifacts"],
        }

    # --- narrative validation --------------------------------------------

    @staticmethod
    def _parse_and_validate_narrative(raw: str) -> dict:
        """Parse the agent's JSON string into the six validated narrative fields."""
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip()
            if text[:4].lower() == "json":
                text = text[4:]
        try:
            data = json.loads(text)
        except Exception as exc:
            raise InvalidClosureNarrativeError(
                f"The closure report agent did not return valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise InvalidClosureNarrativeError(
                "Closure narrative JSON must be a single object."
            )
        out: dict = {}
        for field in _NARRATIVE_FIELDS:
            value = data.get(field)
            if value is None or not str(value).strip():
                raise InvalidClosureNarrativeError(
                    f"Closure narrative is missing required field '{field}'."
                )
            out[field] = str(value).strip()
        return out

    # --- metadata / provenance helpers ---------------------------------

    @staticmethod
    def _derive_metadata_from_brd(brd_text: str) -> ProjectMetadata:
        """Best-effort project metadata pulled from the BRD's own header block."""
        title = _TITLE_PATTERN.search(brd_text)
        if title:
            project_name = title.group(1).strip()
        else:
            any_h1 = _ANY_H1_PATTERN.search(brd_text)
            project_name = any_h1.group(1).strip() if any_h1 else "the project"

        client = _CLIENT_PATTERN.search(brd_text)
        project_type = _PROJECT_TYPE_PATTERN.search(brd_text)
        return ProjectMetadata(
            project_name=project_name,
            client_name=client.group(1).strip() if client else "the client",
            project_type=project_type.group(1).strip() if project_type else "the described system",
            industry="the domain described in the project artifacts",
        )

    @staticmethod
    def _selected_versions(evidence: dict) -> dict:
        """The version numbers this report is built from: final for BRD/HLD/LLD/
        Test Cases, latest for User Stories (no finalization stage)."""
        art = evidence["artifact_status"]
        return {
            "brd": art["brd"]["final_version"],
            "hld": art["hld"]["final_version"],
            "lld": art["lld"]["final_version"],
            "us": art["user_stories"]["latest_version"],
            "tc": art["test_cases"]["final_version"],
        }

    @staticmethod
    def _format_source_ref(sel: dict) -> str:
        def _tok(v):
            return str(v) if v is not None else "none"
        return (
            f"brd_v{_tok(sel['brd'])};hld_v{_tok(sel['hld'])};lld_v{_tok(sel['lld'])};"
            f"us_v{_tok(sel['us'])};tc_v{_tok(sel['tc'])}"
        )

    @staticmethod
    def _built_from_line(sel: dict) -> str:
        def _v(v, absent):
            return f"v{v}" if v is not None else absent
        return (
            f"BRD {_v(sel['brd'], 'not finalized')}, "
            f"HLD {_v(sel['hld'], 'not finalized')}, "
            f"LLD {_v(sel['lld'], 'not finalized')}, "
            f"User Stories {_v(sel['us'], 'not available')}, "
            f"Test Cases {_v(sel['tc'], 'not finalized')}, "
            f"plus the project Traceability and Quality reports"
        )

    def _next_version_number(self) -> int:
        existing = self._cr.get_all_versions()
        return (existing[-1].version + 1) if existing else 1

    # --- Markdown rendering (deterministic facts + narrative prose) --------

    def _render_markdown(
        self, evidence: dict, status: str, narrative: dict, version: int,
        *, source_label: str,
    ) -> str:
        art = evidence["artifact_status"]
        rt = evidence["requirements_traceability"]
        us = evidence["user_story_summary"]
        tc = evidence["test_coverage_summary"]
        qf = evidence["quality_findings"]
        sel = self._selected_versions(evidence)
        readiness = self._readiness_phrase(status)
        blockers = evidence["blockers"]
        outstanding = evidence["outstanding_items"]
        summary = evidence["evidence_summary"]
        meta = self._derive_metadata_from_brd(self._brd.get_final_version().content)

        L: list[str] = []

        def line(s: str = "") -> None:
            L.append(s)

        def bullets(items: list[str], empty: str) -> None:
            if items:
                for it in items:
                    line(f"- {it}")
            else:
                line(f"- {empty}")
            line()

        # --- header block ---
        line(f"# {meta.project_name} — Project Closure Report")
        line()
        line(f"**Version:** {version}")
        line(f"**Source:** {source_label}")
        line(f"**Built From:** {self._built_from_line(sel)}")
        line(f"**Closure Status:** {status}")
        line(f"**Closure Readiness:** {readiness}")
        line(f"**Client:** {meta.client_name}")
        line(f"**Project Type:** {meta.project_type}")
        line(f"**Generated:** {evidence['generated_on']}")
        line()

        # --- 1. Project Closure Summary ---
        line("## 1. Project Closure Summary")
        line()
        line(f"**Project ID:** {evidence['project_id']}")
        line(f"**Closure Status:** {status}")
        line(f"**Closure Readiness:** {readiness}")
        line()
        line(narrative["executive_summary"])
        line()

        # --- 2. Scope Summary ---
        line("## 2. Scope Summary")
        line()
        line(narrative["scope_summary"])
        line()

        # --- 3. SDLC Artifact Summary ---
        line("## 3. SDLC Artifact Summary")
        line()
        line("| Artifact | Exists | Latest Version | Final Version | Status | Evidence Reference |")
        line("| --- | --- | --- | --- | --- | --- |")
        line(f"| SOW | No | — | — | Not tracked | {_UNAVAILABLE} |")
        for key, label in (
            ("brd", "BRD"), ("hld", "HLD"), ("user_stories", "User Stories"),
            ("lld", "LLD"), ("test_cases", "Test Cases"),
        ):
            info = art[key]
            latest = f"v{info['latest_version']}" if info["latest_version"] is not None else "—"
            if not info["final_stage"]:
                final = "N/A (no finalization stage)"
            elif info["final_version"] is not None:
                final = f"v{info['final_version']}"
            else:
                final = "None"
            evref = f"{key}/versions.json" if info["exists"] else _UNAVAILABLE
            line(f"| {label} | {'Yes' if info['exists'] else 'No'} | {latest} | "
                 f"{final} | {info['status']} | {evref} |")
        line()
        line("Version selection:")
        line()
        bullets(evidence["version_selection"], "No artifacts available.")

        # --- 4. Requirements & Traceability ---
        line("## 4. Requirements & Traceability")
        line()
        line(f"**Total requirements:** {rt['total_requirements']}")
        line(f"**Requirements with user stories:** {rt['requirements_with_user_stories']}")
        line(f"**Requirements with test cases:** {rt['requirements_with_test_cases']}")
        line(f"**Covered requirements:** {rt['covered_requirements']}")
        line(f"**Uncovered requirements:** {len(rt['uncovered_requirements'])}"
             + (f" ({', '.join(map(str, rt['uncovered_requirements']))})"
                if rt['uncovered_requirements'] else ""))
        line(f"**Requirement coverage:** {rt['coverage_pct']}%")
        line()
        by_kind = rt.get("coverage_by_kind", {})
        if by_kind:
            line("| Requirement kind | Total | Covered | Coverage % |")
            line("| --- | --- | --- | --- |")
            for kind in ("FR", "NFR", "BR"):
                k = by_kind.get(kind)
                if k:
                    line(f"| {kind} | {k['total']} | {k['covered']} | {k['coverage_pct']}% |")
            for kind, k in by_kind.items():
                if kind not in ("FR", "NFR", "BR"):
                    line(f"| {kind} | {k['total']} | {k['covered']} | {k['coverage_pct']}% |")
            line()

        # --- 5. User Story Summary ---
        line("## 5. User Story Summary")
        line()
        line(f"**Total user stories:** {us['total_user_stories']}")
        line(f"**Stories covered by test cases:** {us['stories_covered_by_test_cases']}")
        line(f"**Uncovered user stories:** {len(us['uncovered_user_stories'])}"
             + (f" ({', '.join(map(str, us['uncovered_user_stories']))})"
                if us['uncovered_user_stories'] else ""))
        line(f"**Story-to-test coverage:** {us['story_to_test_coverage_pct']}%")
        line()

        # --- 6. Test Coverage Summary ---
        line("## 6. Test Coverage Summary")
        line()
        line(f"**Total test cases:** {tc['total_test_cases']}")
        pop = tc["requirement_or_story_ref_populated"]
        line(f"**Test cases with a requirement/user-story reference:** "
             f"{pop['populated']} of {pop['total']}")
        line(f"**Covered requirements with a directly-cited test case:** "
             f"{tc['covered_requirements_with_direct_test_case']}")
        line(f"**Covered requirements whose only test-case evidence is via a "
             f"user story:** {tc['covered_requirements_via_user_story_only']}")
        line(f"(These two are mutually exclusive and sum to the "
             f"{rt['covered_requirements']} covered requirements in Section 4.)")
        line()

        # --- 7. Quality Findings (reused from the Project Quality Report) ---
        line("## 7. Quality Findings")
        line()
        line("Reused from the project Traceability and Quality reports (no figures "
             "are recomputed here).")
        line()
        line(f"**Ungrounded test-case references:** {qf['grounding_findings']['total']}")
        line(f"**Orphan references:** {qf['orphan_references']['total']}")
        line(f"**Uncovered requirements:** {len(qf['uncovered_requirements'])}")
        line(f"**Uncovered user stories:** {len(qf['uncovered_user_stories'])}")
        line(f"**Missing / non-final required artifacts:** "
             + (", ".join(qf["missing_final_artifacts"]) or "none"))
        line()
        line(narrative["findings_summary"])
        line()

        # --- 8. Risks / Gaps / Outstanding Items ---
        line("## 8. Risks, Gaps and Outstanding Items")
        line()
        line(f"Closure status **{status}** — {_STATUS_GLOSS.get(status, '')}")
        line()
        line("**Blocking issues** — gaps that make the project not ready to close; "
             "a non-empty list forces the status to NOT_READY_FOR_CLOSURE:")
        line()
        bullets(blockers, "None — no issue prevents closure.")
        line("**Outstanding non-blocking items** — objective coverage / evidence "
             "gaps recorded for human review; they do **not** block closure:")
        line()
        bullets(outstanding, "None identified from project evidence.")
        line(narrative["outstanding_items_summary"])
        line()

        # --- 9. Final Closure Assessment ---
        line("## 9. Final Closure Assessment")
        line()
        line(f"**Closure Status:** {status}")
        line(f"**Closure Readiness:** {readiness}")
        line(f"**Blocking issues:** {len(blockers)} &nbsp;&nbsp; "
             f"**Outstanding non-blocking items:** {len(outstanding)}")
        line()
        line("**Blocking issues:**")
        line()
        bullets(blockers, "None.")
        line("**Outstanding non-blocking items:**")
        line()
        bullets(outstanding, "None.")
        line("**Evidence summary:**")
        line()
        for k, v in summary.items():
            pretty = k.replace("_", " ").capitalize()
            if isinstance(v, list):
                v = ", ".join(map(str, v)) or "none"
            line(f"- {pretty}: {v}")
        line()
        line("**Limitations:**")
        line()
        for lim in _LIMITATIONS:
            line(f"- {lim}")
        line()
        line(narrative["closure_summary"])
        line()
        line("_This is an evidence-based assessment of the SDLC artifacts the "
             "platform has persisted. It is not a substitute for the BRD, HLD, "
             "LLD, test cases, traceability matrix, or Project Quality Report, and "
             "it must be confirmed by a human reviewer before formal closure._")

        return "\n".join(L).rstrip() + "\n"

    # --- generation / regeneration --------------------------------------

    def _generate(self, *, source_label: str) -> BRDVersion:
        self._guard_unlocked()
        brd = self._require_final_brd()

        evidence = self._assemble_evidence()
        status = self._decide_closure_status(evidence)
        evidence["closure_status"] = status
        evidence["closure_readiness"] = self._readiness_phrase(status)
        evidence["blockers"] = self._blockers(evidence, status)
        evidence["outstanding_items"] = self._outstanding_items(evidence)
        evidence["evidence_summary"] = self._evidence_summary(evidence)
        evidence["limitations"] = list(_LIMITATIONS)

        metadata = self._derive_metadata_from_brd(brd.content)
        raw = self._agent.synthesize_narrative(
            evidence_json=json.dumps(evidence, ensure_ascii=False, indent=2),
            closure_status=status,
            metadata=metadata,
        )
        narrative = self._parse_and_validate_narrative(raw)

        n = self._next_version_number()
        content = self._render_markdown(
            evidence, status, narrative, n, source_label=source_label
        )
        sel = self._selected_versions(evidence)
        note = (
            f"{source_label} (status: {status}); built from "
            f"{self._built_from_line(sel)}"
        )
        return self._cr.add_version(
            content=content,
            source=("initial" if n == 1 else "ai_refine"),
            note=note,
            source_ref=self._format_source_ref(sel),
        )

    def generate(self) -> BRDVersion:
        """Generate the project closure report from current project evidence.

        Blocked only if there is no accepted BRD (`NoFinalBRDError`) or the final
        closure report is locked. Every other missing/incomplete piece of
        evidence is represented inside the report and reflected in the
        deterministic closure status. Creates a new version; never finalizes.
        """
        return self._generate(source_label="Generated from project evidence")

    def regenerate(self) -> BRDVersion:
        """Rebuild the closure report from the CURRENT project evidence.

        Appends a NEW version to the same stream; never mutates a prior version
        and never finalizes. Behaviourally identical to `generate()` - kept as a
        distinct name so call sites and the UI read clearly.
        """
        return self._generate(source_label="Regenerated from project evidence")

    # --- version history / finalization (human-controlled) ---------------

    def get_all_versions(self) -> list[BRDVersion]:
        return self._cr.get_all_versions()

    def get_version(self, version_number: int) -> BRDVersion | None:
        return self._cr.get_version(version_number)

    def get_latest(self) -> BRDVersion | None:
        return self._cr.get_latest_version()

    def has_versions(self) -> bool:
        return bool(self._cr.get_all_versions())

    def choose_final(self, version_number: int) -> BRDVersion:
        return self._cr.mark_final(version_number)

    def unlock_final(self) -> BRDVersion | None:
        return self._cr.unlock_final()

    def get_final(self) -> BRDVersion | None:
        return self._cr.get_final_version()

    def is_locked(self) -> bool:
        final = self._cr.get_final_version()
        return bool(final and final.is_locked)

    # --- provenance / staleness (Phase 10B; live, never stored, no auto-regen) ----

    def recorded_source_versions(self) -> dict | None:
        """The BRD/HLD/LLD/US/TC versions the LATEST closure report was built from.

        Parsed from that version's composite `source_ref`
        (`brd_v{b};hld_v{h};lld_v{l};us_v{u};tc_v{t}`). Returns e.g.
        `{"brd": 1, "hld": 1, "lld": 1, "us": 2, "tc": 1}` — `None` for a token
        means that artifact was absent (or not finalized, for BRD/HLD/LLD/TC)
        when the report was generated. Returns `None` when there is no closure
        report or its `source_ref` is not the composite form.
        """
        latest = self._cr.get_latest_version()
        if latest is None or not latest.source_ref or ";" not in latest.source_ref:
            return None
        parsed: dict = {}
        for key, raw in _REF_TOKEN_PATTERN.findall(latest.source_ref):
            parsed[key] = None if raw == "none" else int(raw)
        if "brd" not in parsed:
            return None
        for k in ("hld", "lld", "us", "tc"):
            parsed.setdefault(k, None)
        return parsed

    def current_source_versions(self) -> dict:
        """The versions the Closure Report's evidence WOULD select right now.

        Uses the report's EXISTING evidence semantics (see `_selected_versions`):
        the accepted/final version for BRD / HLD / LLD / Test Cases, and the
        LATEST version for User Stories (which are not independently finalized).
        `None` where that artifact is absent / not finalized.
        """
        brd = self._brd.get_final_version()
        hld = self._hld.get_final_version()
        lld = self._lld.get_final_version()
        us = self._us.get_latest_version()
        tc = self._tc.get_final_version()
        return {
            "brd": brd.version if brd else None,
            "hld": hld.version if hld else None,
            "lld": lld.version if lld else None,
            "us": us.version if us else None,
            "tc": tc.version if tc else None,
        }

    def stale_sources(self) -> list[str]:
        """Which upstream artifacts changed since the latest closure report.

        Compares `recorded_source_versions()` against `current_source_versions()`
        (the report's own evidence-selection semantics). A change in ANY of the
        five is reported — including a `None -> vN` transition, because the
        closure report's artifact summary and deterministic status would
        genuinely differ if regenerated now (unlike Test Cases, where HLD/LLD are
        pure optional context). Traceability and the Project Quality Report are
        never considered stale — they are recomputed live every time the report
        is (re)generated.

        Returns names from `["BRD", "HLD", "LLD", "User Stories", "Test Cases"]`.
        Empty list when there is no closure report or nothing changed. This is a
        non-blocking signal: it never regenerates, finalizes, or unlocks.
        """
        recorded = self.recorded_source_versions()
        if recorded is None:
            return []
        current = self.current_source_versions()
        labels = {
            "brd": "BRD", "hld": "HLD", "lld": "LLD",
            "us": "User Stories", "tc": "Test Cases",
        }
        return [labels[k] for k in ("brd", "hld", "lld", "us", "tc")
                if recorded[k] != current[k]]

    def is_stale(self) -> bool:
        return bool(self.stale_sources())
