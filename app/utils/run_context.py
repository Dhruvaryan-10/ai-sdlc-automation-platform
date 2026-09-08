"""
Phase 13A — pipeline run correlation (stdlib only, zero `app` imports).

A `run_step()` invocation mints a UUID4 `run_id` and enters `run_context(...)`;
every log line and every LLM telemetry record emitted while that context is
active carries the same `run_id` (and the run's `project_id`). Outside a
pipeline run the values default to `"-"`.

Mechanism: `contextvars.ContextVar`. Chosen because —
  * it is per-thread / per-async-task, so concurrent Streamlit sessions (a
    thread each) never share a run id;
  * LangGraph 1.2.11 copies the calling context into its sync-node executor
    (verified), so the concurrent HLD ∥ User-Story fan-out nodes inherit the
    id without threading it through `SDLCState` or any public signature;
  * `run_context()` uses `set()` / `reset(token)`, so the id is restored (not
    just cleared) when the run finishes and never leaks into the next one;
  * direct service/agent calls made outside `run_step()` simply see the `"-"`
    default — no caller has to pass anything.

This module is imported by `app.utils.logger` and `app.utils.metrics`; keeping
it dependency-free avoids an import cycle.
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from typing import Iterator

_MISSING = "-"

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("sdlc_run_id", default=_MISSING)
_project_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sdlc_project_id", default=_MISSING
)


def current_run_id() -> str:
    """The active pipeline run id, or ``"-"`` outside a run. Never raises."""
    try:
        return _run_id.get()
    except LookupError:  # pragma: no cover - default makes this unreachable
        return _MISSING


def current_project_id() -> str:
    """The active run's project id, or ``"-"``. Never raises."""
    try:
        return _project_id.get()
    except LookupError:  # pragma: no cover
        return _MISSING


def new_run_id() -> str:
    """A fresh pipeline run id (UUID4 string)."""
    return str(uuid.uuid4())


def new_generation_id() -> str:
    """A fresh id for one LLM provider invocation/attempt (UUID4 hex)."""
    return uuid.uuid4().hex


@contextmanager
def run_context(run_id: str, project_id: str = _MISSING) -> Iterator[None]:
    """Bind `run_id` / `project_id` for the duration of the ``with`` block.

    Restores the previous values on exit (including on exception), so sequential
    runs never share an id and a nested run would correctly nest/restore.
    """
    run_token = _run_id.set(run_id or _MISSING)
    proj_token = _project_id.set(project_id or _MISSING)
    try:
        yield
    finally:
        _run_id.reset(run_token)
        _project_id.reset(proj_token)
