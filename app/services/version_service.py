"""
Version Service.

WHY: The spec requires full version history (never overwrite, every edit
creates a new version, one version can be marked "final"). Phase 1 explicitly
excludes databases, so we persist this as a JSON file per project under
outputs/{project_id}/versions.json. The service layer is written so that
swapping this for a real DB later (Phase 2+) only means rewriting this one
class — nothing above it needs to change.

Phase 2 note: this class is document-agnostic. The Business Analyst Agent uses
it for BRD versions at outputs/{project_id}/versions.json; the Solution
Architect Agent reuses the exact same class for HLD versions at
outputs/{project_id}/hld/versions.json by passing `subdir="hld"`. The two
streams are fully isolated — each instance only ever reads/writes its own file.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

VersionSource = Literal["initial", "manual_edit", "ai_refine"]


class BRDVersion(BaseModel):
    """One immutable version record. Shared by the BRD and HLD version streams.

    `source_ref` is optional free-form provenance (e.g. the HLD stores
    "brd_v3" to record which accepted BRD it was generated from). BRD versions
    leave it as None.
    """

    version: int
    content: str
    source: VersionSource
    created_at: str
    note: str = ""
    is_final: bool = False
    is_locked: bool = False
    source_ref: str | None = None


class VersionService:
    """Manages the version history of a single project's BRD."""

    def __init__(
        self,
        project_id: str,
        output_dir: Path | None = None,
        *,
        subdir: str | None = None,
    ):
        self.project_id = project_id
        base_dir = output_dir or settings.resolved_output_dir()
        project_dir = base_dir / project_id
        # A subdir isolates a second version stream (e.g. "hld") from the BRD
        # stream while keeping the exact same file layout and behaviour.
        self._project_dir = project_dir / subdir if subdir else project_dir
        self._project_dir.mkdir(parents=True, exist_ok=True)
        self._versions_file = self._project_dir / "versions.json"

    # --- persistence helpers -------------------------------------------------

    def _load_all(self) -> list[BRDVersion]:
        if not self._versions_file.exists():
            return []
        raw = json.loads(self._versions_file.read_text(encoding="utf-8"))
        return [BRDVersion(**item) for item in raw]

    def _save_all(self, versions: list[BRDVersion]) -> None:
        self._versions_file.write_text(
            json.dumps([v.model_dump() for v in versions], indent=2),
            encoding="utf-8",
        )

    # --- public API ------------------------------------------------------------

    def add_version(
        self,
        content: str,
        source: VersionSource,
        note: str = "",
        source_ref: str | None = None,
    ) -> BRDVersion:
        """Append a new version. Never mutates or overwrites existing versions."""
        versions = self._load_all()
        next_number = (versions[-1].version + 1) if versions else 1

        new_version = BRDVersion(
            version=next_number,
            content=content,
            source=source,
            created_at=datetime.utcnow().isoformat(),
            note=note,
            source_ref=source_ref,
        )
        versions.append(new_version)
        self._save_all(versions)

        logger.info(f"Project '{self.project_id}': created version {next_number} (source={source})")
        return new_version

    def get_all_versions(self) -> list[BRDVersion]:
        return self._load_all()

    def get_version(self, version_number: int) -> BRDVersion | None:
        for v in self._load_all():
            if v.version == version_number:
                return v
        return None

    def get_latest_version(self) -> BRDVersion | None:
        versions = self._load_all()
        return versions[-1] if versions else None

    def mark_final(self, version_number: int) -> BRDVersion:
        """Mark one version as the official Final BRD (and lock it).

        Only the `is_final` / `is_locked` flags are touched — the stored
        `content` of every version is left untouched, so accepting a version
        never rewrites history.
        """
        versions = self._load_all()
        target = None

        for v in versions:
            if v.version == version_number:
                v.is_final = True
                v.is_locked = True
                target = v
            else:
                v.is_final = False
                v.is_locked = False

        if target is None:
            raise ValueError(f"Version {version_number} does not exist for project '{self.project_id}'")

        self._save_all(versions)
        logger.info(f"Project '{self.project_id}': version {version_number} marked as FINAL BRD (locked)")
        return target

    def unlock_final(self) -> BRDVersion | None:
        """Unlock the accepted BRD so further edits/refinements are allowed again.

        The version REMAINS marked as final and its content is unchanged — only
        the lock is released. Any subsequent edit creates a brand new version,
        leaving the accepted one intact in history.
        """
        versions = self._load_all()
        target = None

        for v in versions:
            if v.is_final:
                v.is_locked = False
                target = v

        if target is None:
            logger.warning(f"Project '{self.project_id}': unlock requested but no final version exists")
            return None

        self._save_all(versions)
        logger.info(f"Project '{self.project_id}': final BRD (v{target.version}) unlocked for further editing")
        return target

    def get_final_version(self) -> BRDVersion | None:
        for v in self._load_all():
            if v.is_final:
                return v
        return None
