"""
Solution Architect Service (Phase 2).

WHY: This is the single orchestration point for the HLD workflow, mirroring
BusinessAnalystService:
    generate -> require FINAL BRD -> agent -> HLD version 1
    manual edit / AI refine -> agent -> HLD version N
    mark final / lock / unlock

HARD INPUT RULE: an HLD is only ever generated from the accepted/final BRD
(is_final=True). Never from the SOW, an unaccepted BRD, or the latest draft. If
no final BRD exists, generation is blocked with NoFinalBRDError.

The UI talks only to this service (and the docx generator) — never to the agent
or the version service directly. HLD versions are stored independently from BRD
versions at outputs/{project_id}/hld/versions.json via the same VersionService.
"""

import re

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.solution_architect.agent import SolutionArchitectAgent
from app.services.version_service import BRDVersion, VersionService
from app.services.version_text import stamp_version_number
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TITLE_PATTERN = re.compile(r"^#\s+(.+?)\s+[—\-–]\s+Business Requirement Document", re.MULTILINE)
_CLIENT_PATTERN = re.compile(r"\*\*Client:\*\*\s*(.+)")
_PROJECT_TYPE_PATTERN = re.compile(r"\*\*Project Type:\*\*\s*(.+)")
_ANY_H1_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)


class NoFinalBRDError(Exception):
    """Raised when HLD generation is attempted without an accepted/final BRD."""


class HLDLockedError(Exception):
    """Raised when an edit/refinement is attempted while the final HLD is locked."""


class SolutionArchitectService:
    """Orchestrates the full (final BRD) -> HLD workflow for a single project."""

    def __init__(
        self,
        project_id: str,
        ba_service: BusinessAnalystService | None = None,
        agent: SolutionArchitectAgent | None = None,
    ):
        self.project_id = project_id
        self._ba_service = ba_service or BusinessAnalystService(project_id=project_id)
        self._agent = agent or SolutionArchitectAgent()
        self._version_service = VersionService(project_id=project_id, subdir="hld")

    # --- BRD gate ------------------------------------------------------------

    def _require_final_brd(self) -> BRDVersion:
        """Return the accepted/final BRD, or raise if there isn't one.

        Lock state is intentionally ignored: a BRD that was finalized and later
        unlocked for editing is still the accepted source document.
        """
        final_brd = self._ba_service.get_final_brd()
        if final_brd is None:
            raise NoFinalBRDError("Accept a BRD before generating the HLD.")
        return final_brd

    @staticmethod
    def _derive_metadata_from_brd(brd_text: str) -> ProjectMetadata:
        """Best-effort project metadata pulled from the BRD's own header block.

        Project details are collected once at BRD generation and not persisted,
        so by the time an HLD is generated (possibly a later session) they are
        only recoverable from the BRD document itself. The BRD is the grounding
        source anyway; anything not found here falls back to a neutral value the
        prompt can work with.
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

    # --- step 1: generate HLD v1 ------------------------------------------------

    def generate_initial_hld(self) -> BRDVersion:
        """Generate HLD version 1 from the accepted/final BRD. Blocked if none exists."""
        final_brd = self._require_final_brd()
        metadata = self._derive_metadata_from_brd(final_brd.content)

        hld_text = self._agent.generate_hld(final_brd.content, metadata)
        hld_text = stamp_version_number(hld_text, version_number=1)

        return self._version_service.add_version(
            content=hld_text,
            source="initial",
            note=f"Generated from accepted BRD v{final_brd.version}",
            source_ref=f"brd_v{final_brd.version}",
        )

    # --- step 2a: manual edit ------------------------------------------------------

    def save_manual_edit(self, edited_content: str, note: str = "Manual edit") -> BRDVersion:
        if self.is_locked():
            raise HLDLockedError(
                "The final HLD is locked. Unlock it before making further changes."
            )
        if not edited_content or not edited_content.strip():
            raise ValueError("Cannot save an empty HLD")
        edited_content = stamp_version_number(
            edited_content, version_number=self._next_version_number()
        )
        return self._version_service.add_version(
            content=edited_content, source="manual_edit", note=note
        )

    # --- step 2b: AI refine ------------------------------------------------------------

    def refine_with_ai(self, user_feedback: str) -> BRDVersion:
        if self.is_locked():
            raise HLDLockedError(
                "The final HLD is locked. Unlock it before refining further."
            )
        latest = self._version_service.get_latest_version()
        if latest is None:
            raise ValueError("No existing HLD version to refine. Generate an initial HLD first.")

        refined_text = self._agent.refine_hld(
            current_hld=latest.content,
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

    def choose_final_hld(self, version_number: int) -> BRDVersion:
        return self._version_service.mark_final(version_number)

    def unlock_final_hld(self) -> BRDVersion | None:
        return self._version_service.unlock_final()

    def get_final_hld(self) -> BRDVersion | None:
        return self._version_service.get_final_version()

    def is_locked(self) -> bool:
        final = self._version_service.get_final_version()
        return bool(final and final.is_locked)

    # --- stale-vs-BRD hint (display only, no dependency tracking) --------------------

    def brd_changed_since_hld(self) -> bool:
        """True when the accepted BRD version differs from the one the HLD was built on.

        This is a non-blocking display hint only. It does not invalidate,
        regenerate, or delete anything — the HLD stays independently versioned.
        """
        hld_versions = self._version_service.get_all_versions()
        if not hld_versions:
            return False
        final_brd = self._ba_service.get_final_brd()
        if final_brd is None:
            return False
        return hld_versions[0].source_ref != f"brd_v{final_brd.version}"

    def source_brd_version(self) -> int | None:
        """The BRD version number this HLD was generated from, if recorded."""
        hld_versions = self._version_service.get_all_versions()
        if not hld_versions or not hld_versions[0].source_ref:
            return None
        match = re.search(r"brd_v(\d+)", hld_versions[0].source_ref)
        return int(match.group(1)) if match else None
