"""
QA / Test Case Service (Phase 6).

WHY: The single orchestration point for the test-case workflow, mirroring the
earlier services:
    generate  -> require FINAL BRD (+ optional HLD/LLD/User Stories) -> agent -> v1
    regenerate / manual edit / AI refine -> agent/user -> version N
    mark final / lock / unlock

DEPENDENCY MODEL:
    Final BRD  (REQUIRED — primary source of truth)
    Accepted HLD          (OPTIONAL context)
    Accepted LLD          (OPTIONAL context)
    Final/refined or latest User Stories  (OPTIONAL context)
        -> QA / Test Case Agent (returns JSON)
        -> service validates the JSON, renders it to a Markdown test-case document,
           and appends a new version to the OWN test-case stream

STORAGE: test cases live in their own append-only stream at
outputs/{project_id}/test_cases/versions.json via the shared VersionService
(subdir "test_cases"). The stored `content` is a Markdown document (same
convention as BRD/HLD/LLD/User Stories) rendered deterministically from the
agent's JSON. The QA Agent never writes to any other stream and never creates a
second copy of BRD/HLD/LLD/User-Story data.

INDEPENDENCE: this module reads the BRD / HLD / LLD / user-story streams ONLY
through the shared VersionService interface. It imports no other agent package's
implementation (only the shared ProjectMetadata value type + the
stamp_version_number helper), and nothing in those packages depends on this one.

STALENESS: each test-case version records the BRD / HLD / LLD / User-Story
versions actually used, as a composite `source_ref`. Optional artifacts that
were NOT used are recorded as "none". Staleness is computed live (never stored,
never auto-regenerating):
  * BRD is stale iff its accepted version changed.
  * HLD/LLD/User Stories are stale ONLY iff they were used (recorded as an int)
    AND their authoritative version subsequently changed. A previously-absent
    optional artifact appearing later does NOT make an existing version stale.
"""

import json
import re

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.test_case.agent import TestCaseAgent
from app.services.version_service import BRDVersion, VersionService
from app.services.version_text import stamp_version_number
from app.utils.logger import get_logger

logger = get_logger(__name__)

_NO_HLD_SENTINEL = "(no accepted HLD available)"
_NO_LLD_SENTINEL = "(no accepted LLD available)"
_NO_US_SENTINEL = "(no user stories available)"

_TC_ID_PATTERN = re.compile(r"^TC-\d{3}$")
_TITLE_PATTERN = re.compile(
    r"^#\s+(.+?)\s+[—\-–]\s+Business Requirement Document", re.MULTILINE
)
_CLIENT_PATTERN = re.compile(r"\*\*Client:\*\*\s*(.+)")
_PROJECT_TYPE_PATTERN = re.compile(r"\*\*Project Type:\*\*\s*(.+)")
_ANY_H1_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_REF_TOKEN_PATTERN = re.compile(r"(brd|hld|lld|us)_v(\d+|none)")

_REQUIRED_FIELDS = (
    "id", "title", "requirement_or_story_ref",
    "test_steps", "expected_result", "priority", "test_type",
)

# A leading "1. " / "2) " / "3 ) " style step number the model may still emit
# inside a test_steps item — stripped so _render_markdown owns the numbering.
_STEP_PREFIX_PATTERN = re.compile(r"^\s*\d+\s*[.)]\s+")


class NoFinalBRDError(Exception):
    """Raised when test-case generation is attempted without an accepted/final BRD.

    Local to this package (same shape as the other agents' gate errors).
    """


class TestCaseLockedError(Exception):
    """Raised when an edit/refinement is attempted while the final test cases are locked."""

    __test__ = False  # not a pytest test class despite the "Test" prefix


class InvalidTestCaseJSONError(ValueError):
    """Raised when the agent's output is not the expected test-case JSON."""

    __test__ = False


class TestCaseService:
    """Orchestrates BRD (+ optional context) -> test cases for a single project."""

    __test__ = False  # not a pytest test class despite the "Test" prefix

    def __init__(self, project_id: str, agent: TestCaseAgent | None = None):
        self.project_id = project_id
        self._agent = agent or TestCaseAgent()
        # Required source.
        self._brd = VersionService(project_id=project_id)
        # Optional context (read-only).
        self._hld = VersionService(project_id=project_id, subdir="hld")
        self._lld = VersionService(project_id=project_id, subdir="lld")
        self._us = VersionService(project_id=project_id, subdir="user_stories")
        # Own stream (read + write).
        self._tc = VersionService(project_id=project_id, subdir="test_cases")

    # --- prerequisites --------------------------------------------------------

    def _require_final_brd(self) -> BRDVersion:
        final_brd = self._brd.get_final_version()
        if final_brd is None:
            raise NoFinalBRDError("Accept a BRD before generating test cases.")
        return final_brd

    def _guard_unlocked(self) -> None:
        final = self._tc.get_final_version()
        if final and final.is_locked:
            raise TestCaseLockedError(
                "The final test cases are locked. Unlock them before making further changes."
            )

    def _gather_optional(self):
        """The authoritative optional-context versions, or None when unavailable."""
        hld = self._hld.get_final_version()
        lld = self._lld.get_final_version()
        us = self._us.get_final_version() or self._us.get_latest_version()
        return hld, lld, us

    # --- helpers -----------------------------------------------------------

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
    def _format_source_ref(brd_v: int, hld_v: int | None, lld_v: int | None, us_v: int | None) -> str:
        return (
            f"brd_v{brd_v};"
            f"hld_v{hld_v if hld_v is not None else 'none'};"
            f"lld_v{lld_v if lld_v is not None else 'none'};"
            f"us_v{us_v if us_v is not None else 'none'}"
        )

    @staticmethod
    def _built_from_line(brd_v: int, hld_v: int | None, lld_v: int | None, us_v: int | None) -> str:
        def _v(x):
            return f"v{x}" if x is not None else "unavailable"

        return (
            f"BRD v{brd_v}, HLD {_v(hld_v)}, LLD {_v(lld_v)}, User Stories {_v(us_v)}"
        )

    @staticmethod
    def _parse_and_validate(raw: str) -> list[dict]:
        """Parse the agent's JSON string into a validated list of test-case dicts."""
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip()
            if text[:4].lower() == "json":
                text = text[4:]
        try:
            data = json.loads(text)
        except Exception as exc:
            raise InvalidTestCaseJSONError(
                f"The QA agent did not return valid JSON: {exc}"
            ) from exc

        cases = data.get("test_cases") if isinstance(data, dict) else data
        if not isinstance(cases, list) or not cases:
            raise InvalidTestCaseJSONError(
                "Test-case JSON must contain a non-empty 'test_cases' list."
            )

        seen: set[str] = set()
        for i, case in enumerate(cases, start=1):
            if not isinstance(case, dict):
                raise InvalidTestCaseJSONError(f"Test case #{i} is not an object.")
            for field in _REQUIRED_FIELDS:
                if field not in case or case[field] in (None, "", []):
                    raise InvalidTestCaseJSONError(
                        f"Test case #{i} is missing required field '{field}'."
                    )
            cid = str(case["id"]).strip()
            if not _TC_ID_PATTERN.match(cid):
                raise InvalidTestCaseJSONError(
                    f"Test case id '{cid}' must have the form TC-000."
                )
            if cid in seen:
                raise InvalidTestCaseJSONError(f"Duplicate test case id '{cid}'.")
            seen.add(cid)
        return cases

    @staticmethod
    def _render_markdown(
        cases: list[dict],
        *,
        project_name: str,
        client_name: str,
        project_type: str,
        version: int,
        source_label: str,
        built_from: str,
    ) -> str:
        """Deterministically render validated test-case dicts into a Markdown document.

        Same document conventions as BRD/HLD/LLD/User Stories: an H1 title, a
        '**Key:** value' header block (incl. the application-managed **Version:**,
        **Source:**, **Built From:** lines), then one '## TC-NNN — Title' section
        per test case with '**Field:**' lines and '-' bullet lists.
        """

        def _bullets(label: str, items) -> list[str]:
            if not items:
                return []
            if isinstance(items, str):
                items = [items]
            out = [f"**{label}:**"]
            out += [f"- {str(it).strip()}" for it in items]
            out.append("")
            return out

        def _numbered(label: str, items) -> list[str]:
            """A real ordered Markdown list. Strips any leading step number the
            model still put in an item so the numbering is never doubled."""
            if not items:
                return []
            if isinstance(items, str):
                items = [items]
            out = [f"**{label}:**"]
            for n, it in enumerate(items, start=1):
                text = _STEP_PREFIX_PATTERN.sub("", str(it).strip()).strip()
                out.append(f"{n}. {text}")
            out.append("")
            return out

        lines: list[str] = [
            f"# {project_name} — Test Cases",
            "",
            f"**Version:** {version}",
            f"**Source:** {source_label}",
            f"**Built From:** {built_from}",
            f"**Client:** {client_name}",
            f"**Project Type:** {project_type}",
            "",
        ]

        for case in cases:
            lines.append(f"## {case['id']} — {str(case.get('title', '')).strip()}")
            lines.append("")
            ref = case.get("requirement_or_story_ref")
            if ref:
                lines.append(f"**Requirement / User Story Reference:** {ref}")
            for key, label in (
                ("brd_reference", "BRD Reference"),
                ("user_story_reference", "User Story Reference"),
                ("hld_reference", "HLD Reference"),
                ("lld_reference", "LLD Reference"),
            ):
                val = case.get(key)
                if val:
                    lines.append(f"**{label}:** {val}")
            lines.append(f"**Priority:** {case.get('priority', '')}")
            lines.append(f"**Test Type:** {case.get('test_type', '')}")
            deps = case.get("dependencies") or []
            if deps:
                lines.append(f"**Dependencies:** {', '.join(str(d) for d in deps)}")
            lines.append("")
            lines += _bullets("Preconditions", case.get("preconditions"))
            lines += _bullets("Test Data", case.get("test_data"))
            lines += _numbered("Test Steps", case.get("test_steps"))
            lines += ["**Expected Result:**", str(case.get("expected_result", "")).strip(), ""]
            notes = str(case.get("notes") or "").strip()
            if notes:
                lines += ["**Notes:**", notes, ""]

        return "\n".join(lines).rstrip() + "\n"

    def _next_version_number(self) -> int:
        existing = self._tc.get_all_versions()
        return (existing[-1].version + 1) if existing else 1

    # --- generation / regeneration / refinement ---------------------------------

    def generate(self) -> BRDVersion:
        """Generate a test-case version from the currently available artifacts.

        Blocked if there is no accepted BRD or the final test cases are locked.
        Optional HLD/LLD/User Stories are used when present and recorded in
        provenance; their absence never blocks generation.
        """
        self._guard_unlocked()
        brd = self._require_final_brd()
        hld, lld, us = self._gather_optional()
        metadata = self._derive_metadata_from_brd(brd.content)

        raw = self._agent.generate_test_cases(
            brd_text=brd.content,
            hld_text=hld.content if hld else _NO_HLD_SENTINEL,
            lld_text=lld.content if lld else _NO_LLD_SENTINEL,
            user_stories_text=us.content if us else _NO_US_SENTINEL,
            metadata=metadata,
        )
        return self._commit(raw, brd, hld, lld, us, metadata, source_label="Generated from artifacts")

    def regenerate(self) -> BRDVersion:
        """Fresh generation from the CURRENT artifacts, ignoring current test-case content.

        Appends a NEW version to the same stream; never mutates prior versions.
        Behaviourally identical to `generate()` — kept as a distinct name so call
        sites and the UI read clearly.
        """
        return self.generate()

    def refine_with_ai(self, user_feedback: str) -> BRDVersion:
        """Apply reviewer feedback to the latest test-case version. Creates a new version."""
        self._guard_unlocked()
        latest = self._tc.get_latest_version()
        if latest is None:
            raise ValueError("No existing test cases to refine. Generate them first.")

        brd = self._require_final_brd()
        hld, lld, us = self._gather_optional()
        metadata = self._derive_metadata_from_brd(brd.content)

        raw = self._agent.refine_test_cases(
            current_test_cases=latest.content,
            user_feedback=user_feedback,
            current_version=latest.version,
            brd_text=brd.content,
            hld_text=hld.content if hld else _NO_HLD_SENTINEL,
            lld_text=lld.content if lld else _NO_LLD_SENTINEL,
            user_stories_text=us.content if us else _NO_US_SENTINEL,
            metadata=metadata,
        )
        return self._commit(
            raw, brd, hld, lld, us, metadata,
            source_label="Artifact-refined", note_prefix="Refined",
        )

    @staticmethod
    def _reference_is_grounded(ref: str, *artifact_texts: str) -> bool:
        """True if `ref` (or a lenient variant) appears in any supplied artifact text."""
        ref = str(ref).strip()
        if not ref:
            return True
        candidates = {ref}
        # "Section 3.2" / "§4" also count as grounded if a bare "3.2" / "4" is present.
        bare = re.sub(r"^(section|sec\.?|§)\s+", "", ref, flags=re.IGNORECASE).strip()
        if bare:
            candidates.add(bare)
        lowered = [t.lower() for t in artifact_texts if t]
        return any(c.lower() in text for c in candidates for text in lowered)

    def _check_traceability(self, cases, brd_text, hld_text, lld_text, us_text) -> None:
        """Non-blocking: log a warning for any cited reference not found in its
        source artifact. Never raises, never drops a test case, never changes
        versioning/persistence — a debugging aid only."""
        checks = (
            ("requirement_or_story_ref", (brd_text, us_text), "BRD/User Stories"),
            ("brd_reference", (brd_text,), "BRD"),
            ("user_story_reference", (us_text,), "User Stories"),
            ("hld_reference", (hld_text,), "HLD"),
            ("lld_reference", (lld_text,), "LLD"),
        )
        for case in cases:
            cid = case.get("id", "?")
            for field, texts, label in checks:
                val = case.get(field)
                if val in (None, "", []):
                    continue
                if not any(t for t in texts):
                    continue  # that optional artifact was not supplied — nothing to check
                if not self._reference_is_grounded(val, *texts):
                    logger.warning(
                        "Traceability: %s %s=%r not found in the supplied %s text",
                        cid, field, val, label,
                    )

    def _commit(
        self, raw, brd, hld, lld, us, metadata, *,
        source_label: str, note_prefix: str = "Generated",
    ) -> BRDVersion:
        cases = self._parse_and_validate(raw)
        self._check_traceability(
            cases,
            brd.content,
            hld.content if hld else None,
            lld.content if lld else None,
            us.content if us else None,
        )
        n = self._next_version_number()
        hld_v = hld.version if hld else None
        lld_v = lld.version if lld else None
        us_v = us.version if us else None

        content = self._render_markdown(
            cases,
            project_name=metadata.project_name,
            client_name=metadata.client_name,
            project_type=metadata.project_type,
            version=n,
            source_label=source_label,
            built_from=self._built_from_line(brd.version, hld_v, lld_v, us_v),
        )

        note = f"{note_prefix} from BRD v{brd.version}"
        if hld_v:
            note += f", HLD v{hld_v}"
        if lld_v:
            note += f", LLD v{lld_v}"
        if us_v:
            note += f", User Stories v{us_v}"
        unavailable = [lab for lab, v in (("HLD", hld_v), ("LLD", lld_v), ("User Stories", us_v)) if v is None]
        if unavailable:
            note += f" ({', '.join(unavailable)}: unavailable)"

        return self._tc.add_version(
            content=content,
            source=("initial" if n == 1 else "ai_refine"),
            note=note,
            source_ref=self._format_source_ref(brd.version, hld_v, lld_v, us_v),
        )

    # --- manual edit -----------------------------------------------------------

    def save_manual_edit(self, edited_content: str, note: str = "Manual edit") -> BRDVersion:
        self._guard_unlocked()
        if not edited_content or not edited_content.strip():
            raise ValueError("Cannot save empty test cases")
        if "## TC-" not in edited_content:
            raise ValueError(
                "The edited test cases must contain at least one '## TC-NNN' section."
            )
        latest = self._tc.get_latest_version()
        content = stamp_version_number(edited_content, self._next_version_number())
        return self._tc.add_version(
            content=content,
            source="manual_edit",
            note=note,
            # a hand edit doesn't change which artifacts the cases are based on
            source_ref=latest.source_ref if latest else None,
        )

    # --- version history / finalization -----------------------------------------

    def get_all_versions(self) -> list[BRDVersion]:
        return self._tc.get_all_versions()

    def get_version(self, version_number: int) -> BRDVersion | None:
        return self._tc.get_version(version_number)

    def has_versions(self) -> bool:
        return bool(self._tc.get_all_versions())

    def choose_final(self, version_number: int) -> BRDVersion:
        return self._tc.mark_final(version_number)

    def unlock_final(self) -> BRDVersion | None:
        return self._tc.unlock_final()

    def get_final(self) -> BRDVersion | None:
        return self._tc.get_final_version()

    def is_locked(self) -> bool:
        final = self._tc.get_final_version()
        return bool(final and final.is_locked)

    # --- provenance / staleness (live, never stored, no auto-regen) --------------

    def recorded_source_versions(self) -> dict | None:
        """The BRD/HLD/LLD/US versions the latest test-case version was built from.

        Returns e.g. {"brd": 2, "hld": 1, "lld": None, "us": 3}, or None when
        there is no test-case version / no parseable provenance. `None` for an
        optional key means that artifact was NOT used at generation time.
        """
        latest = self._tc.get_latest_version()
        if latest is None or not latest.source_ref or ";" not in latest.source_ref:
            return None
        parsed: dict = {}
        for key, raw in _REF_TOKEN_PATTERN.findall(latest.source_ref):
            parsed[key] = None if raw == "none" else int(raw)
        if "brd" not in parsed:
            return None
        for k in ("hld", "lld", "us"):
            parsed.setdefault(k, None)
        return parsed

    def current_source_versions(self) -> dict:
        """The current authoritative version numbers of every source (None if absent)."""
        brd = self._brd.get_final_version()
        hld = self._hld.get_final_version()
        lld = self._lld.get_final_version()
        us = self._us.get_final_version() or self._us.get_latest_version()
        return {
            "brd": brd.version if brd else None,
            "hld": hld.version if hld else None,
            "lld": lld.version if lld else None,
            "us": us.version if us else None,
        }

    def stale_sources(self) -> list[str]:
        """Which source artifacts changed since the latest test-case version.

        BRD: stale iff its accepted version changed. HLD/LLD/User Stories: stale
        ONLY iff they were used (recorded as an int) AND their authoritative
        version subsequently changed. A previously-absent optional artifact
        appearing later is NOT stale.
        """
        recorded = self.recorded_source_versions()
        if recorded is None:
            return []
        current = self.current_source_versions()
        if current["brd"] is None:
            return []

        changed: list[str] = []
        if recorded["brd"] != current["brd"]:
            changed.append("BRD")
        if recorded["hld"] is not None and recorded["hld"] != current["hld"]:
            changed.append("HLD")
        if recorded["lld"] is not None and recorded["lld"] != current["lld"]:
            changed.append("LLD")
        if recorded["us"] is not None and recorded["us"] != current["us"]:
            changed.append("User Stories")
        return changed

    def is_stale(self) -> bool:
        return bool(self.stale_sources())
