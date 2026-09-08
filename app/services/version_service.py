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

Phase 12A hardening (persistence reliability only — no API / semantic change):
  * `versions.json` is now written ATOMICALLY: a fresh temp file in the SAME
    directory is flushed + fsynced, then `os.replace()`'d over the target, so a
    crash mid-write can never leave a truncated primary file.
  * A last-known-good copy is kept at `versions.json.bak`, refreshed from the
    current (valid) primary immediately before each write. It is never the
    primary source during normal operation and is never written on a read.
  * If the primary is unreadable (JSON corruption / schema-validation failure /
    read error) it is transparently RECOVERED from the backup — logged at
    CRITICAL, never silent — and the primary is healed. If neither the primary
    nor the backup is usable, a `VersionPersistenceError` is raised rather than
    silently returning an empty history. A genuinely new/empty stream (no file
    at all) still returns `[]`, not an error.
  * The read → modify → save cycle for a given `versions.json` path is
    serialized by an in-process re-entrant lock (one per resolved path, shared
    across every `VersionService` targeting it), so concurrent writers in the
    same process cannot lose an update. Per-path + re-entrant => no deadlock for
    nested or cross-stream `VersionService` calls.
  * `created_at` is now a timezone-aware UTC ISO-8601 timestamp.

The JSON record shape, every public method signature, version numbering,
append-only history, finalization / unlock behaviour, `source_ref`, and
stream / project isolation are all unchanged.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

VersionSource = Literal["initial", "manual_edit", "ai_refine"]

_BACKUP_SUFFIX = ".bak"

# Failures that mean "this file is not a usable version history": a broken file
# (`json.JSONDecodeError` subclasses `ValueError`), a record that fails schema
# validation (`pydantic.ValidationError` subclasses `ValueError` here), or a
# structurally wrong top level (`TypeError` from `**` / iteration). `OSError`
# (file vanished / unreadable) is caught alongside these at each call site.
_CORRUPT_FILE_ERRORS = (ValueError, TypeError)


class VersionPersistenceError(RuntimeError):
    """A version stream's `versions.json` is unreadable AND no usable
    `versions.json.bak` is available to recover from.

    Deliberately NOT an `OSError` subclass: retrying the same call will not fix a
    corrupt file, so callers should treat it as a hard, distinct failure rather
    than a transient disk error.
    """


# --- in-process load -> modify -> save serialization --------------------------
# One re-entrant lock per resolved `versions.json` path, shared by every
# VersionService instance that targets that path. Re-entrant so a public mutator
# that holds the lock can still call the private `_load_all` / `_save_all`
# helpers (which take the same lock) without deadlocking; per-path so operations
# on different streams never block each other and there is no lock-ordering cycle
# even for nested `VersionService` calls.
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


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
        self._backup_file = self._versions_file.with_name(
            self._versions_file.name + _BACKUP_SUFFIX
        )
        # Identifier for log lines only — never any document content or secret.
        self._stream_label = project_id if subdir is None else f"{project_id}/{subdir}"
        self._lock = _lock_for(self._versions_file)

    # --- (de)serialization (pure) ------------------------------------------

    @staticmethod
    def _serialize(versions: list[BRDVersion]) -> str:
        # Byte-identical to the pre-12A on-disk format: a 2-space-indented JSON
        # array, no trailing newline, UTF-8. Downstream "stream untouched" byte
        # comparisons depend on this staying exactly the same.
        return json.dumps([v.model_dump() for v in versions], indent=2)

    @staticmethod
    def _deserialize(text: str) -> list[BRDVersion]:
        raw = json.loads(text)
        if not isinstance(raw, list):
            raise TypeError("versions.json top level must be a JSON array")
        return [BRDVersion(**item) for item in raw]

    @staticmethod
    def _utc_now_iso() -> str:
        """Timezone-aware UTC timestamp, ISO 8601 (e.g. '...T12:34:56.789+00:00')."""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _summarize_error(err: BaseException) -> str:
        """A short, content-free description of a failure, for logging."""
        return f"{type(err).__name__}: {err}"[:200]

    # --- atomic disk primitives ------------------------------------------

    def _atomic_write_text(self, target: Path, text: str) -> None:
        """Write `text` to `target` atomically.

        A fresh uniquely-named temp file in the SAME directory is written,
        flushed and fsynced, then `os.replace()`'d over `target` (an atomic
        rename on POSIX and NTFS). On any failure the temp file is removed and
        `target` is left exactly as it was.
        """
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._fsync_dir(target.parent)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Best-effort fsync of `directory` so the rename metadata is durable.

        A no-op where the platform will not let us open a directory fd
        (e.g. Windows) — `os.replace` is still atomic there.
        """
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _read_versions_or_none(self, path: Path) -> list[BRDVersion] | None:
        """Parse `path` into a version list, or return None if it is absent /
        unreadable / corrupt. Never raises for a bad file."""
        try:
            if not path.exists():
                return None
            return self._deserialize(path.read_text(encoding="utf-8"))
        except (OSError, *_CORRUPT_FILE_ERRORS):
            return None

    # --- persistence -----------------------------------------------------

    def _load_all(self) -> list[BRDVersion]:
        with self._lock:
            if not self._versions_file.exists():
                # Genuinely new / empty stream — NOT an error; backup not consulted.
                return []
            try:
                return self._deserialize(
                    self._versions_file.read_text(encoding="utf-8")
                )
            except (OSError, *_CORRUPT_FILE_ERRORS) as primary_err:
                return self._recover_from_backup(primary_err)

    def _recover_from_backup(self, primary_err: BaseException) -> list[BRDVersion]:
        """The primary `versions.json` exists but is unusable. Recover from
        `versions.json.bak` if it is valid (and heal the primary), otherwise
        raise `VersionPersistenceError`. Always logged at CRITICAL — never
        silent."""
        recovered = self._read_versions_or_none(self._backup_file)
        if recovered is None:
            logger.critical(
                "VersionService[%s]: 'versions.json' is unreadable (%s) and no "
                "usable '%s' is available; this stream's version history cannot "
                "be loaded.",
                self._stream_label,
                self._summarize_error(primary_err),
                self._backup_file.name,
            )
            raise VersionPersistenceError(
                f"Version history for '{self._stream_label}' is corrupt and could "
                f"not be recovered from a backup."
            ) from primary_err

        # Backup is valid: promote it back to the primary (atomically) so later
        # reads/writes build on the recovered state, then report the recovery.
        try:
            self._atomic_write_text(
                self._versions_file,
                self._backup_file.read_text(encoding="utf-8"),
            )
            healed = "the primary file was restored from it"
        except OSError as restore_err:
            healed = (
                f"the primary file could NOT be restored "
                f"({self._summarize_error(restore_err)}) — the next successful "
                f"save will heal it"
            )
        logger.critical(
            "VersionService[%s]: 'versions.json' was unreadable (%s); recovered "
            "%d version(s) from '%s' and %s.",
            self._stream_label,
            self._summarize_error(primary_err),
            len(recovered),
            self._backup_file.name,
            healed,
        )
        return recovered

    def _save_all(self, versions: list[BRDVersion]) -> None:
        """Persist `versions` atomically.

        Ordering (see the module docstring's failure-safety contract):
          1. Serialize the full list. A serialization failure aborts before any
             disk write — the previous state is untouched.
          2. If a current, VALID primary exists, refresh `versions.json.bak`
             from it (atomically). A corrupt primary is NOT copied over a good
             backup. A backup-refresh failure aborts the write — we never
             overwrite the last good copy without a safety net in place.
          3. Atomically replace the primary with the new content.
        At every instant at least one complete, valid file (primary or backup)
        exists.
        """
        new_text = self._serialize(versions)
        with self._lock:
            if self._versions_file.exists():
                try:
                    current_text = self._versions_file.read_text(encoding="utf-8")
                    self._deserialize(current_text)  # validate; result discarded
                    primary_is_valid = True
                except (OSError, *_CORRUPT_FILE_ERRORS):
                    primary_is_valid = False
                    current_text = ""
                if primary_is_valid:
                    try:
                        self._atomic_write_text(self._backup_file, current_text)
                    except OSError as exc:
                        logger.error(
                            "VersionService[%s]: could not refresh '%s' before "
                            "saving (%s); aborting the write to keep the existing "
                            "version history intact.",
                            self._stream_label,
                            self._backup_file.name,
                            self._summarize_error(exc),
                        )
                        raise
            try:
                self._atomic_write_text(self._versions_file, new_text)
            except OSError as exc:
                logger.error(
                    "VersionService[%s]: failed to persist 'versions.json' (%s); "
                    "the previous version history is unchanged.",
                    self._stream_label,
                    self._summarize_error(exc),
                )
                raise

    # --- public API ------------------------------------------------------------

    def add_version(
        self,
        content: str,
        source: VersionSource,
        note: str = "",
        source_ref: str | None = None,
    ) -> BRDVersion:
        """Append a new version. Never mutates or overwrites existing versions."""
        with self._lock:
            versions = self._load_all()
            next_number = (versions[-1].version + 1) if versions else 1

            new_version = BRDVersion(
                version=next_number,
                content=content,
                source=source,
                created_at=self._utc_now_iso(),
                note=note,
                source_ref=source_ref,
            )
            versions.append(new_version)
            self._save_all(versions)

            logger.info(
                f"Project '{self.project_id}': created version {next_number} (source={source})"
            )
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
        with self._lock:
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
                raise ValueError(
                    f"Version {version_number} does not exist for project '{self.project_id}'"
                )

            self._save_all(versions)
            logger.info(
                f"Project '{self.project_id}': version {version_number} marked as FINAL BRD (locked)"
            )
        return target

    def unlock_final(self) -> BRDVersion | None:
        """Unlock the accepted BRD so further edits/refinements are allowed again.

        The version REMAINS marked as final and its content is unchanged — only
        the lock is released. Any subsequent edit creates a brand new version,
        leaving the accepted one intact in history.
        """
        with self._lock:
            versions = self._load_all()
            target = None

            for v in versions:
                if v.is_final:
                    v.is_locked = False
                    target = v

            if target is None:
                logger.warning(
                    f"Project '{self.project_id}': unlock requested but no final version exists"
                )
                return None

            self._save_all(versions)
            logger.info(
                f"Project '{self.project_id}': final BRD (v{target.version}) unlocked for further editing"
            )
        return target

    def get_final_version(self) -> BRDVersion | None:
        for v in self._load_all():
            if v.is_final:
                return v
        return None
