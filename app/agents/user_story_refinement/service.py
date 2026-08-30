"""
User Story Refinement Service (Phase 5).

WHY: The single orchestration point for the user-story *refinement* workflow.
Phase 3 answered "given the BRD, what user stories should exist?". Phase 5 answers
"given the existing user stories, the BRD, and the available architecture/design,
how should those stories be improved so they are complete, consistent, traceable,
and implementation-ready?".

SINGLE SOURCE OF TRUTH: refined user stories are appended to the EXISTING Phase 3
user-story version stream at outputs/{project_id}/user_stories/versions.json via
the shared VersionService. There is NO second user-story history system. This
service reuses the same append-only VersionService the Initial User Story Agent
uses; every prior version stays byte-for-byte unchanged.

DEPENDENCY MODEL:
    Final BRD  (REQUIRED — primary business source of truth)
    Existing user stories  (REQUIRED — the starting point; latest version, whatever
                            its origin: initial generation, manual edit, or a
                            previous refinement)
    Accepted HLD  (OPTIONAL context)
    Accepted LLD  (OPTIONAL context)
        -> User Story Refinement Agent
        -> a new user-story version

INDEPENDENCE: this module reads the BRD / HLD / LLD / user-story streams ONLY
through the shared VersionService interface. It imports no other agent package
(not the Initial User Story, Solution Architect, or Low-Level Design packages),
and nothing in those packages depends on this one.

STALENESS: each refinement version records the BRD / source-story / HLD / LLD
versions it was built from, as a composite `source_ref`. If the accepted BRD,
HLD, or LLD later changes, the existing refined stories are *flagged* stale
(computed live, never stored) — they are never mutated, regenerated, or deleted.
Only an explicit re-refinement (`refine()` again) produces a fresh version.
"""

import re

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.user_story_refinement.agent import UserStoryRefinementAgent
from app.services.version_service import BRDVersion, VersionService
from app.services.version_text import stamp_version_number
from app.utils.logger import get_logger

logger = get_logger(__name__)

_NO_HLD_SENTINEL = "(no accepted HLD available)"
_NO_LLD_SENTINEL = "(no accepted LLD available)"

# The user-story document header (see the Phase 3 generate_user_stories.txt
# template): "# <name> — Draft User Stories", "**Client:** ...", "**Project Type:** ...".
# Allow a "Refined"/"Draft"/plain "User Stories" title so re-refined docs still match.
_TITLE_PATTERN = re.compile(
    r"^#\s+(.+?)\s+[—\-–]\s+(?:Draft |Refined )?User Stories", re.MULTILINE
)
_CLIENT_PATTERN = re.compile(r"\*\*Client:\*\*\s*(.+)")
_PROJECT_TYPE_PATTERN = re.compile(r"\*\*Project Type:\*\*\s*(.+)")
_ANY_H1_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# Composite source_ref token, e.g. "brd_v3;us_v1;hld_v2;lld_vnone".
_REF_TOKEN_PATTERN = re.compile(r"(brd|us|hld|lld)_v(\d+|none)")


class NoFinalBRDError(Exception):
    """Raised when refinement is attempted without an accepted/final BRD.

    Local to this package (same shape as the other agents' gate errors) so the
    refinement branch owns its own gate error without cross-agent coupling.
    """


class NoInitialUserStoriesError(Exception):
    """Raised when refinement is attempted before any user-story version exists."""


class RefinementLockedError(Exception):
    """Raised when refinement is attempted while the final user stories are locked."""


class UserStoryRefinementService:
    """Orchestrates artifact-based refinement of the existing user-story stream."""

    def __init__(self, project_id: str, agent: UserStoryRefinementAgent | None = None):
        self.project_id = project_id
        self._agent = agent or UserStoryRefinementAgent()
        # The user-story stream is read AND written here — the same append-only
        # stream the Initial User Story Agent owns.
        self._stories = VersionService(project_id=project_id, subdir="user_stories")
        # Read-only views of the other three artifact streams.
        self._brd = VersionService(project_id=project_id)
        self._hld = VersionService(project_id=project_id, subdir="hld")
        self._lld = VersionService(project_id=project_id, subdir="lld")

    # --- prerequisites ----------------------------------------------------------

    def _require_final_brd(self) -> BRDVersion:
        final_brd = self._brd.get_final_version()
        if final_brd is None:
            raise NoFinalBRDError("Accept a BRD before refining the user stories.")
        return final_brd

    def _require_existing_stories(self) -> BRDVersion:
        latest = self._stories.get_latest_version()
        if latest is None:
            raise NoInitialUserStoriesError(
                "Generate the initial user stories before refining them."
            )
        return latest

    def _guard_unlocked(self) -> None:
        final = self._stories.get_final_version()
        if final and final.is_locked:
            raise RefinementLockedError(
                "The final user stories are locked. Unlock them before refining."
            )

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _derive_metadata_from_stories(stories_text: str) -> ProjectMetadata:
        """Best-effort project metadata from the user-story document's own header."""
        title = _TITLE_PATTERN.search(stories_text)
        if title:
            project_name = title.group(1).strip()
        else:
            any_h1 = _ANY_H1_PATTERN.search(stories_text)
            project_name = any_h1.group(1).strip() if any_h1 else "the project"

        client = _CLIENT_PATTERN.search(stories_text)
        project_type = _PROJECT_TYPE_PATTERN.search(stories_text)

        return ProjectMetadata(
            project_name=project_name,
            client_name=client.group(1).strip() if client else "the client",
            project_type=project_type.group(1).strip() if project_type else "the described system",
            industry="the domain described in the project artifacts",
        )

    @staticmethod
    def _format_source_ref(brd_v: int, us_v: int, hld_v: int | None, lld_v: int | None) -> str:
        return (
            f"brd_v{brd_v};us_v{us_v};"
            f"hld_v{hld_v if hld_v is not None else 'none'};"
            f"lld_v{lld_v if lld_v is not None else 'none'}"
        )

    @staticmethod
    def _parse_refinement_ref(version: BRDVersion | None) -> dict | None:
        """Parse a refinement version's composite source_ref, or None if it isn't one.

        A refinement version has source == "ai_refine" AND a composite source_ref
        (contains ';'). Phase 3's freeform `refine_with_ai` leaves source_ref None,
        and initial generation uses a single "brd_v{n}" token, so neither matches.
        """
        if version is None or version.source != "ai_refine" or not version.source_ref:
            return None
        if ";" not in version.source_ref:
            return None
        parsed: dict = {}
        for key, raw in _REF_TOKEN_PATTERN.findall(version.source_ref):
            parsed[key] = None if raw == "none" else int(raw)
        if "brd" not in parsed or "us" not in parsed:
            return None
        parsed.setdefault("hld", None)
        parsed.setdefault("lld", None)
        return parsed

    def _next_version_number(self) -> int:
        existing = self._stories.get_all_versions()
        return (existing[-1].version + 1) if existing else 1

    # --- refinement ---------------------------------------------------------

    def refine(self) -> BRDVersion:
        """Refine the latest user-story version against the BRD (+ optional HLD/LLD).

        Creates a NEW user-story version. Never mutates previous versions. Blocked
        if there is no accepted BRD, no existing user stories, or the final user
        stories are locked.
        """
        self._guard_unlocked()
        final_brd = self._require_final_brd()
        source_stories = self._require_existing_stories()

        final_hld = self._hld.get_final_version()
        final_lld = self._lld.get_final_version()

        metadata = self._derive_metadata_from_stories(source_stories.content)

        refined_text = self._agent.refine_user_stories(
            current_stories=source_stories.content,
            brd_text=final_brd.content,
            hld_text=final_hld.content if final_hld else _NO_HLD_SENTINEL,
            lld_text=final_lld.content if final_lld else _NO_LLD_SENTINEL,
            metadata=metadata,
            current_version=source_stories.version,
        )
        refined_text = stamp_version_number(refined_text, self._next_version_number())

        note = f"Refined from accepted BRD v{final_brd.version}"
        if final_hld:
            note += f", HLD v{final_hld.version}"
        if final_lld:
            note += f", LLD v{final_lld.version}"
        note += f" (source stories v{source_stories.version})"

        source_ref = self._format_source_ref(
            final_brd.version,
            source_stories.version,
            final_hld.version if final_hld else None,
            final_lld.version if final_lld else None,
        )

        return self._stories.add_version(
            content=refined_text,
            source="ai_refine",
            note=note,
            source_ref=source_ref,
        )

    # --- staleness (computed live, never stored) --------------------------------

    def recorded_source_versions(self) -> dict | None:
        """The BRD/US/HLD/LLD versions the latest refinement was built from, if any.

        Returns e.g. {"brd": 3, "us": 1, "hld": 2, "lld": None}, or None when the
        latest user-story version was not produced by artifact refinement.
        """
        return self._parse_refinement_ref(self._stories.get_latest_version())

    def is_refined_latest(self) -> bool:
        """True when the latest user-story version came from artifact refinement."""
        return self.recorded_source_versions() is not None

    def stale_sources(self) -> list[str]:
        """Which of BRD / HLD / LLD have changed since the latest refinement.

        A non-blocking display signal only. Empty list when the latest version is
        not a refinement version, or when nothing has changed.
        """
        recorded = self.recorded_source_versions()
        if recorded is None:
            return []
        current_brd = self._brd.get_final_version()
        if current_brd is None:
            return []
        current_hld = self._hld.get_final_version()
        current_lld = self._lld.get_final_version()

        changed: list[str] = []
        if recorded["brd"] != current_brd.version:
            changed.append("BRD")
        if recorded["hld"] != (current_hld.version if current_hld else None):
            changed.append("HLD")
        if recorded["lld"] != (current_lld.version if current_lld else None):
            changed.append("LLD")
        return changed

    def is_stale(self) -> bool:
        """True when any source artifact changed since the latest refinement.

        One stale state; `stale_sources()` itemises which sources changed.
        """
        return bool(self.stale_sources())
