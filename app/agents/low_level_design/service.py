"""
Low-Level Design Service (Phase 4).

WHY: The single orchestration point for the LLD workflow, mirroring
BusinessAnalystService / SolutionArchitectService / InitialUserStoryService:
    generate -> require FINAL HLD -> agent -> LLD version 1
    manual edit / AI refine -> agent -> LLD version N
    mark final / lock / unlock

HARD INPUT RULE: an LLD is only ever generated from the accepted/final HLD
(is_final=True). Draft-only HLD is not sufficient. If no final HLD exists,
generation is blocked with NoFinalHLDError.

DEPENDENCY MODEL:
    accepted BRD -> Solution Architect -> HLD -> LLD Agent
                    Draft User Stories -> LLD Agent (context only, OPTIONAL)

- The accepted/final HLD is the ONLY hard prerequisite.
- The BRD is available supporting business context.
- Draft user stories, IF they exist, are read as optional functional context.
  They are NOT a prerequisite for generation or finalization.

INDEPENDENCE: this module must NOT import the Initial User Story Agent package
and must NOT construct its service. It reads the user-story artifact stream
through the shared VersionService interface (subdir "user_stories"), the same
persistence abstraction every phase uses. LLD versions are stored independently
at outputs/{project_id}/lld/versions.json.

The UI talks only to this service (and the docx generator) — never to the agent
or the version service directly.
"""

import re

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.low_level_design.agent import LowLevelDesignAgent
from app.agents.solution_architect.service import SolutionArchitectService
from app.services.version_service import BRDVersion, VersionService
from app.services.version_text import stamp_version_number
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TITLE_PATTERN = re.compile(r"^#\s+(.+?)\s+[—\-–]\s+High-Level Design", re.MULTILINE)
_CLIENT_PATTERN = re.compile(r"\*\*Client:\*\*\s*(.+)")
_PROJECT_TYPE_PATTERN = re.compile(r"\*\*Project Type:\*\*\s*(.+)")
_ANY_H1_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)

_NO_BRD_SENTINEL = "(no accepted BRD available)"
_NO_USER_STORIES_SENTINEL = "(no draft user stories available)"

# Phase 10B: LLD provenance is a composite of the versions actually consumed:
#   "hld_v{h};brd_v{b};us_v{u}"   (b/u = "none" when that context was unavailable)
# Legacy records may still be the HLD-only form "hld_v{n}" — the parser below
# tolerates both.
_REF_TOKEN_PATTERN = re.compile(r"(hld|brd|us)_v(\d+|none)")


class NoFinalHLDError(Exception):
    """Raised when LLD generation is attempted without an accepted/final HLD.

    Deliberately a local exception (same shape/behaviour as
    SolutionArchitectService's NoFinalBRDError) so the LLD branch owns its own
    gate error.
    """


class LLDLockedError(Exception):
    """Raised when an edit/refinement is attempted while the final LLD is locked."""


class LowLevelDesignService:
    """Orchestrates the full (accepted HLD) -> LLD workflow for a single project."""

    def __init__(
        self,
        project_id: str,
        sa_service: SolutionArchitectService | None = None,
        ba_service: BusinessAnalystService | None = None,
        agent: LowLevelDesignAgent | None = None,
    ):
        self.project_id = project_id
        self._sa_service = sa_service or SolutionArchitectService(project_id=project_id)
        self._ba_service = ba_service or BusinessAnalystService(project_id=project_id)
        self._agent = agent or LowLevelDesignAgent()
        self._version_service = VersionService(project_id=project_id, subdir="lld")

    # --- HLD gate ----------------------------------------------------------

    def _require_final_hld(self) -> BRDVersion:
        """Return the accepted/final HLD, or raise if there isn't one.

        Lock state is intentionally ignored: an HLD that was finalized and later
        unlocked for editing is still the accepted source document.
        """
        final_hld = self._sa_service.get_final_hld()
        if final_hld is None:
            raise NoFinalHLDError("Accept an HLD before generating the LLD.")
        return final_hld

    @staticmethod
    def _derive_metadata_from_hld(hld_text: str) -> ProjectMetadata:
        """Best-effort project metadata pulled from the HLD's own header block.

        The HLD is the LLD's direct source, and its header carries the project
        name / client / project type. Anything not found falls back to a neutral
        value the prompt can work with.
        """
        title = _TITLE_PATTERN.search(hld_text)
        if title:
            project_name = title.group(1).strip()
        else:
            any_h1 = _ANY_H1_PATTERN.search(hld_text)
            project_name = any_h1.group(1).strip() if any_h1 else "the project"

        client = _CLIENT_PATTERN.search(hld_text)
        project_type = _PROJECT_TYPE_PATTERN.search(hld_text)

        return ProjectMetadata(
            project_name=project_name,
            client_name=client.group(1).strip() if client else "the client",
            project_type=project_type.group(1).strip() if project_type else "the described system",
            industry="the domain described in the HLD",
        )

    def _load_user_story_context(self) -> BRDVersion | None:
        """The user-story version used as optional LLD context, or None.

        Reads the user-story version stream directly through the shared
        VersionService abstraction — never through InitialUserStoryService.
        Standardized on the LATEST user-story version (Phase 10B: user stories
        are not an independently finalized artifact in the main lifecycle, so a
        `is_final` flag on an older version must NOT override a newer one).
        Returns None when no user stories exist; the caller substitutes a
        sentinel and the LLD is still generated from the HLD + BRD.
        """
        us_versions = VersionService(project_id=self.project_id, subdir="user_stories")
        return us_versions.get_latest_version()

    @staticmethod
    def _format_source_ref(hld_v: int, brd_v: int | None, us_v: int | None) -> str:
        """Composite provenance for a newly generated LLD (Phase 10B)."""
        def _tok(v: int | None) -> str:
            return str(v) if v is not None else "none"

        return f"hld_v{hld_v};brd_v{_tok(brd_v)};us_v{_tok(us_v)}"

    # --- step 1: generate LLD v1 ---------------------------------------------

    def generate_initial_lld(self) -> BRDVersion:
        """Generate an LLD from the accepted HLD (+ BRD / optional user stories).

        Normally LLD version 1, but the service contract is now safe against a
        direct repeat call: the in-document `**Version:**` line is stamped with
        the ACTUAL next version number (never a hard-coded 1), and generation
        through a locked final LLD is refused. Append-only history is preserved;
        the orchestration graph still guards and no-ops when an LLD already
        exists. Provenance (`source_ref`) records every upstream version consumed.
        """
        if self.is_locked():
            raise LLDLockedError(
                "The final LLD is locked. Unlock it before generating a new LLD version."
            )
        final_hld = self._require_final_hld()
        metadata = self._derive_metadata_from_hld(final_hld.content)

        final_brd = self._ba_service.get_final_brd()
        brd_text = final_brd.content if final_brd else _NO_BRD_SENTINEL
        brd_v = final_brd.version if final_brd else None

        stories = self._load_user_story_context()
        user_stories_text = stories.content if stories else _NO_USER_STORIES_SENTINEL
        us_v = stories.version if stories else None

        lld_text = self._agent.generate_lld(
            hld_text=final_hld.content,
            brd_text=brd_text,
            user_stories_text=user_stories_text,
            metadata=metadata,
        )
        n = self._next_version_number()
        lld_text = stamp_version_number(lld_text, version_number=n)

        note = f"Generated from accepted HLD v{final_hld.version}"
        if brd_v is not None:
            note += f", BRD v{brd_v}"
        if us_v is not None:
            note += f", User Stories v{us_v} (context)"

        return self._version_service.add_version(
            content=lld_text,
            source="initial",
            note=note,
            source_ref=self._format_source_ref(final_hld.version, brd_v, us_v),
        )

    # --- step 2a: manual edit ---------------------------------------------------

    def save_manual_edit(self, edited_content: str, note: str = "Manual edit") -> BRDVersion:
        if self.is_locked():
            raise LLDLockedError(
                "The final LLD is locked. Unlock it before making further changes."
            )
        if not edited_content or not edited_content.strip():
            raise ValueError("Cannot save an empty LLD")
        latest = self._version_service.get_latest_version()
        edited_content = stamp_version_number(
            edited_content, version_number=self._next_version_number()
        )
        return self._version_service.add_version(
            content=edited_content,
            source="manual_edit",
            note=note,
            # A hand edit does not change which upstream artifacts the LLD is
            # based on — carry the prior provenance forward (Phase 10B).
            source_ref=latest.source_ref if latest else None,
        )

    # --- step 2b: AI refine ---------------------------------------------------------

    def refine_with_ai(self, user_feedback: str) -> BRDVersion:
        if self.is_locked():
            raise LLDLockedError(
                "The final LLD is locked. Unlock it before refining further."
            )
        latest = self._version_service.get_latest_version()
        if latest is None:
            raise ValueError("No existing LLD version to refine. Generate an initial LLD first.")

        refined_text = self._agent.refine_lld(
            current_lld=latest.content,
            user_feedback=user_feedback,
            current_version=latest.version,
        )
        return self._version_service.add_version(
            content=stamp_version_number(refined_text, self._next_version_number()),
            source="ai_refine",
            note=user_feedback,
            # A freeform refine reworks the SAME LLD against the SAME upstream
            # artifacts — carry the prior provenance forward (Phase 10B).
            source_ref=latest.source_ref,
        )

    def _next_version_number(self) -> int:
        """Deterministic next version number: always max existing + 1."""
        existing = self._version_service.get_all_versions()
        return (existing[-1].version + 1) if existing else 1

    # --- version history / finalization -------------------------------------------

    def get_all_versions(self) -> list[BRDVersion]:
        return self._version_service.get_all_versions()

    def get_version(self, version_number: int) -> BRDVersion | None:
        return self._version_service.get_version(version_number)

    def has_versions(self) -> bool:
        return bool(self._version_service.get_all_versions())

    def choose_final_lld(self, version_number: int) -> BRDVersion:
        return self._version_service.mark_final(version_number)

    def unlock_final_lld(self) -> BRDVersion | None:
        return self._version_service.unlock_final()

    def get_final_lld(self) -> BRDVersion | None:
        return self._version_service.get_final_version()

    def is_locked(self) -> bool:
        final = self._version_service.get_final_version()
        return bool(final and final.is_locked)

    # --- provenance / staleness (Phase 10B; display only, never auto-regenerates) ---

    def recorded_source_versions(self) -> dict | None:
        """The HLD / BRD / User-Story versions this LLD was generated from.

        Read from the FIRST LLD version's `source_ref` (the provenance holder;
        manual edit / AI refine carry it forward unchanged). Tolerates BOTH the
        Phase 10B composite form `hld_v{h};brd_v{b};us_v{u}` and the legacy
        HLD-only form `hld_v{n}`. Returns e.g. `{"hld": 1, "brd": 1, "us": 2}`
        (a missing token -> `None`), or `None` when there is no LLD version or no
        parseable provenance. `None` for `brd`/`us` means "not recorded" (legacy
        record) and MUST NOT be treated as "was absent at generation time".
        """
        lld_versions = self._version_service.get_all_versions()
        if not lld_versions or not lld_versions[0].source_ref:
            return None
        parsed: dict = {}
        for key, raw in _REF_TOKEN_PATTERN.findall(lld_versions[0].source_ref):
            parsed[key] = None if raw == "none" else int(raw)
        if "hld" not in parsed:
            return None
        for k in ("brd", "us"):
            parsed.setdefault(k, None)
        return parsed

    def stale_sources(self) -> list[str]:
        """Which recorded upstream artifacts changed since this LLD was generated.

        Non-blocking display signal only — never invalidates, regenerates, or
        reopens the LLD gate.
          * HLD  — stale iff the current accepted HLD version differs from the
                   recorded one (or an accepted HLD no longer exists).
          * BRD  — stale ONLY iff a BRD version was recorded (composite
                   provenance) AND the current accepted BRD version differs. A
                   legacy `hld_v{n}`-only record never flags BRD.
          * User Stories — stale ONLY iff a US version was recorded AND the
                   current LATEST user-story version differs.
        """
        recorded = self.recorded_source_versions()
        if recorded is None:
            return []

        changed: list[str] = []
        final_hld = self._sa_service.get_final_hld()
        if recorded["hld"] != (final_hld.version if final_hld else None):
            changed.append("HLD")

        if recorded["brd"] is not None:
            final_brd = self._ba_service.get_final_brd()
            if recorded["brd"] != (final_brd.version if final_brd else None):
                changed.append("BRD")

        if recorded["us"] is not None:
            us_latest = VersionService(
                project_id=self.project_id, subdir="user_stories"
            ).get_latest_version()
            if recorded["us"] != (us_latest.version if us_latest else None):
                changed.append("User Stories")

        return changed

    def is_stale(self) -> bool:
        return bool(self.stale_sources())

    # --- stale-vs-HLD hint (kept for backward compatibility) ----------------------

    def hld_changed_since_lld(self) -> bool:
        """True when the accepted HLD version differs from the one the LLD was
        built on. Now delegates to `stale_sources()` so it works with both the
        legacy and the Phase 10B composite `source_ref` forms."""
        return "HLD" in self.stale_sources()

    def source_hld_version(self) -> int | None:
        """The HLD version number this LLD was generated from, if recorded."""
        recorded = self.recorded_source_versions()
        return recorded["hld"] if recorded else None
