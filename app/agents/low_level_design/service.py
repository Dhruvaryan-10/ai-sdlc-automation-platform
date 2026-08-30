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

    def _load_user_story_context(self) -> str | None:
        """Read the most relevant available draft user stories as optional context.

        Reads the user-story version stream directly through the shared
        VersionService abstraction — never through InitialUserStoryService. Prefers
        a version the user marked final, otherwise the latest one. Returns None
        when no user stories exist; the caller substitutes a sentinel and the LLD
        is still generated from the HLD + BRD.
        """
        us_versions = VersionService(project_id=self.project_id, subdir="user_stories")
        chosen = us_versions.get_final_version() or us_versions.get_latest_version()
        return chosen.content if chosen else None

    # --- step 1: generate LLD v1 ---------------------------------------------

    def generate_initial_lld(self) -> BRDVersion:
        """Generate LLD version 1 from the accepted HLD (+ BRD / optional stories)."""
        final_hld = self._require_final_hld()
        metadata = self._derive_metadata_from_hld(final_hld.content)

        final_brd = self._ba_service.get_final_brd()
        brd_text = final_brd.content if final_brd else _NO_BRD_SENTINEL

        stories = self._load_user_story_context()
        user_stories_text = stories if stories else _NO_USER_STORIES_SENTINEL
        used_stories = stories is not None

        lld_text = self._agent.generate_lld(
            hld_text=final_hld.content,
            brd_text=brd_text,
            user_stories_text=user_stories_text,
            metadata=metadata,
        )
        lld_text = stamp_version_number(lld_text, version_number=1)

        note = f"Generated from accepted HLD v{final_hld.version}"
        if used_stories:
            note += " (with draft user stories as context)"

        return self._version_service.add_version(
            content=lld_text,
            source="initial",
            note=note,
            source_ref=f"hld_v{final_hld.version}",
        )

    # --- step 2a: manual edit ---------------------------------------------------

    def save_manual_edit(self, edited_content: str, note: str = "Manual edit") -> BRDVersion:
        if self.is_locked():
            raise LLDLockedError(
                "The final LLD is locked. Unlock it before making further changes."
            )
        if not edited_content or not edited_content.strip():
            raise ValueError("Cannot save an empty LLD")
        edited_content = stamp_version_number(
            edited_content, version_number=self._next_version_number()
        )
        return self._version_service.add_version(
            content=edited_content, source="manual_edit", note=note
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

    # --- stale-vs-HLD hint (display only, no dependency tracking) ------------------

    def hld_changed_since_lld(self) -> bool:
        """True when the accepted HLD version differs from the one the LLD was built on.

        Non-blocking display hint only. It does not invalidate, regenerate, or
        delete anything — the LLD stays independently versioned. BRD and
        user-story changes are intentionally NOT tracked here (see module
        docstring); the BRD->HLD warning already lives in the HLD workspace, and
        user stories are optional context, not a source of record.
        """
        lld_versions = self._version_service.get_all_versions()
        if not lld_versions:
            return False
        final_hld = self._sa_service.get_final_hld()
        if final_hld is None:
            return False
        return lld_versions[0].source_ref != f"hld_v{final_hld.version}"

    def source_hld_version(self) -> int | None:
        """The HLD version number this LLD was generated from, if recorded."""
        lld_versions = self._version_service.get_all_versions()
        if not lld_versions or not lld_versions[0].source_ref:
            return None
        match = re.search(r"hld_v(\d+)", lld_versions[0].source_ref)
        return int(match.group(1)) if match else None
