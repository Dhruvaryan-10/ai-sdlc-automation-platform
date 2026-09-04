"""
Project-id helpers for the Streamlit UI.

WHY: `streamlit_app.py` used to mint a fresh `uuid.uuid4()[:8]` on every session,
so a persisted project under `outputs/<id>/` could never be reopened from the UI.
The "Load Existing Project" controls need two small, pure functions — kept here
(rather than inline in the Streamlit script) so they can be unit-tested without
importing and running the whole UI module.

Nothing here touches VersionService, the services, or the agents. It only
validates an id string and enumerates which project directories already exist.
"""

import re
from pathlib import Path

# An id is one path segment: letters, digits, underscore, hyphen; 1-64 chars.
# This deliberately rejects ".", "..", "/", "\", spaces and empty strings, so a
# value that passes can never escape `outputs/` when used as `base_dir / id`.
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The BRD stream lives at `outputs/<id>/versions.json`; its presence is what
# makes a directory a "project" (sub-streams like hld/ alone don't count).
_ROOT_VERSIONS_FILE = "versions.json"


def sanitize_project_id(raw: str) -> str | None:
    """Return a safe project id, or None if `raw` is not a valid one.

    Valid: stripped, matches ^[A-Za-z0-9_-]{1,64}$ (e.g. "d1801c21", "my-proj_2").
    Rejected: "", whitespace-only, "..", ".", "a/b", "a\\b", "a b", >64 chars.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    return candidate if _SAFE_ID_PATTERN.match(candidate) else None


def list_existing_projects(output_dir: Path) -> list[str]:
    """Sorted ids of directories directly under `output_dir` that hold a BRD stream.

    A directory counts only if it contains a root `versions.json`. Directories
    with just sub-streams (e.g. only `hld/versions.json`), empty directories and
    stray files are ignored. Returns [] if `output_dir` does not exist.
    """
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in output_dir.iterdir()
        if entry.is_dir() and (entry / _ROOT_VERSIONS_FILE).is_file()
    )
