"""
Initial User Story Service (Phase 3).

WHY: The single orchestration point for the draft-user-story workflow, mirroring
BusinessAnalystService / SolutionArchitectService:
    generate -> require FINAL BRD -> agent -> user stories version 1
    manual edit / AI refine -> agent -> user stories version N
    mark final / lock / unlock

HARD INPUT RULE: user stories are only ever generated from the accepted/final BRD
(is_final=True). Never from the SOW, an unaccepted BRD, the latest draft, an HLD,
or an LLD. If no final BRD exists, generation is blocked with NoFinalBRDError.

INDEPENDENCE: this module is an independent branch from the final BRD, parallel to
(and with no import of, or dependency on) the Solution Architect Agent package.
User story versions are stored independently at
outputs/{project_id}/user_stories/versions.json via the shared VersionService.

The UI talks only to this service (and the docx generator) — never to the agent
or the version service directly.
"""

import re

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.agent import InitialUserStoryAgent
from app.services.version_service import BRDVersion, VersionService
from app.services.version_text import stamp_version_number
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TITLE_PATTERN = re.compile(r"^#\s+(.+?)\s+[—\-–]\s+Business Requirement Document", re.MULTILINE)
_CLIENT_PATTERN = re.compile(r"\*\*Client:\*\*\s*(.+)")
_PROJECT_TYPE_PATTERN = re.compile(r"\*\*Project Type:\*\*\s*(.+)")
_ANY_H1_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)


class NoFinalBRDError(Exception):
    """Raised when user story generation is attempted without an accepted/final BRD.

    Deliberately a local exception (not imported from the Solution Architect
    package) so the User Story branch stays structurally independent. Same
    shape/behaviour as SolutionArchitectService's NoFinalBRDError.
    """


class UserStoryLockedError(Exception):
    """Raised when an edit/refinement is attempted while the final stories are locked."""


class InitialUserStoryService:
    """Orchestrates the full (final BRD) -> draft user stories workflow for one project."""

    def __init__(
        self,
        project_id: str,
        ba_service: BusinessAnalystService | None = None,
        agent: InitialUserStoryAgent | None = None,
    ):
        self.project_id = project_id
        self._ba_service = ba_service or BusinessAnalystService(project_id=project_id)
        self._agent = agent or InitialUserStoryAgent()
        self._version_service = VersionService(project_id=project_id, subdir="user_stories")

    # --- BRD gate ------------------------------------------------------------

    def _require_final_brd(self) -> BRDVersion:
        """Return the accepted/final BRD, or raise if there isn't one.

        Lock state is intentionally ignored: a BRD that was finalized and later
        unlocked for editing is still the accepted source document.
        """
        final_brd = self._ba_service.get_final_brd()
        if final_brd is None:
            raise NoFinalBRDError("Accept a BRD before generating user stories.")
        return final_brd

    @staticmethod
    def _derive_metadata_from_brd(brd_text: str) -> ProjectMetadata:
        """Best-effort project metadata pulled from the BRD's own header block.

        Project details are collected once at BRD generation and not persisted,
        so by the time user stories are generated (possibly a later session) they
        are only recoverable from the BRD document itself. The BRD is the
        grounding source anyway; anything not found falls back to a neutral value.
        """
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
            industry="the domain described in the BRD",
        )

    # --- step 1: generate draft user stories v1 -------------------------------

    def generate_initial_stories(self) -> BRDVersion:
        """Generate draft user stories version 1 from the accepted/final BRD."""
        final_brd = self._require_final_brd()
        metadata = self._derive_metadata_from_brd(final_brd.content)

        stories_text = self._agent.generate_stories(final_brd.content, metadata)
        stories_text = stamp_version_number(stories_text, version_number=1)

        return self._version_service.add_version(
            content=stories_text,
            source="initial",
            note=f"Generated from accepted BRD v{final_brd.version}",
            source_ref=f"brd_v{final_brd.version}",
        )

    # --- step 2a: manual edit ------------------------------------------------------

    def save_manual_edit(self, edited_content: str, note: str = "Manual edit") -> BRDVersion:
        if self.is_locked():
            raise UserStoryLockedError(
                "The final user stories are locked. Unlock them before making further changes."
            )
        if not edited_content or not edited_content.strip():
            raise ValueError("Cannot save empty user stories")
        edited_content = stamp_version_number(
            edited_content, version_number=self._next_version_number()
        )
        return self._version_service.add_version(
            content=edited_content, source="manual_edit", note=note
        )

    # --- step 2b: AI refine ------------------------------------------------------------

    def refine_with_ai(self, user_feedback: str) -> BRDVersion:
        if self.is_locked():
            raise UserStoryLockedError(
                "The final user stories are locked. Unlock them before refining further."
            )
        latest = self._version_service.get_latest_version()
        if latest is None:
            raise ValueError(
                "No existing user stories to refine. Generate the initial stories first."
            )

        refined_text = self._agent.refine_stories(
            current_stories=latest.content,
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

    # --- version history / finalization ----------------------------------------------

    def get_all_versions(self) -> list[BRDVersion]:
        return self._version_service.get_all_versions()

    def get_version(self, version_number: int) -> BRDVersion | None:
        return self._version_service.get_version(version_number)

    def has_versions(self) -> bool:
        return bool(self._version_service.get_all_versions())

    def choose_final_stories(self, version_number: int) -> BRDVersion:
        return self._version_service.mark_final(version_number)

    def unlock_final_stories(self) -> BRDVersion | None:
        return self._version_service.unlock_final()

    def get_final_stories(self) -> BRDVersion | None:
        return self._version_service.get_final_version()

    def is_locked(self) -> bool:
        final = self._version_service.get_final_version()
        return bool(final and final.is_locked)

    # --- stale-vs-BRD hint (display only, no dependency tracking) --------------------

    def brd_changed_since_stories(self) -> bool:
        """True when the accepted BRD version differs from the one the stories were built on.

        Non-blocking display hint only. It does not invalidate, regenerate, or
        delete anything — the user stories stay independently versioned.
        """
        story_versions = self._version_service.get_all_versions()
        if not story_versions:
            return False
        final_brd = self._ba_service.get_final_brd()
        if final_brd is None:
            return False
        return story_versions[0].source_ref != f"brd_v{final_brd.version}"

    def source_brd_version(self) -> int | None:
        """The BRD version number these stories were generated from, if recorded."""
        story_versions = self._version_service.get_all_versions()
        if not story_versions or not story_versions[0].source_ref:
            return None
        match = re.search(r"brd_v(\d+)", story_versions[0].source_ref)
        return int(match.group(1)) if match else None
