"""
Per-session duplicate-generation guard for the Streamlit UI.

WHY THIS FILE EXISTS (free-tier stabilisation):
Streamlit reruns the whole script on every click. While a generation button's
handler is blocked inside a long Gemini call, a second click on the same button
is queued by the browser; as soon as the first run finishes, Streamlit starts a
new run in which that button reads ``True`` again — so the work runs twice
(a duplicate BRD version, and a second Gemini request against a 5-requests-per-
minute free-tier quota).

The guard keeps a small record in the caller-supplied mapping (the UI passes
``st.session_state``, so the state is strictly PER SESSION — never module-level,
never shared between users or projects):

  * ``running``       — the operation currently executing (with its start time);
  * ``finished``      — when each operation last finished (success OR failure);
  * ``cooldown_until``— set after Gemini returns a rate limit with a retry delay.

``try_begin(op)`` refuses to start when:
  1. another generation is already marked running in this session;
  2. the same operation finished less than ``DUPLICATE_WINDOW_S`` ago — that is
     the signature of a click queued during the previous run;
  3. Gemini asked us to wait and the window has not passed yet.

``finish(op)`` is always called from a ``finally`` (via ``slot()``), so the
in-progress flag is reset on success, on failure, and when Streamlit interrupts
the run (its control-flow exceptions derive from ``BaseException``).

LIMITATION (by design): this prevents duplicates within ONE browser session.
It is not a global rate limiter — several users (or several tabs) share one
Gemini API key / project quota and are not coordinated here.

Pure: no Streamlit import; the clock is injectable for tests.
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, MutableMapping

STATE_KEY = "_generation_guard"

# A queued click re-runs within milliseconds of the previous run ending; a
# deliberate repeat click this soon after a finished generation is not useful
# (BRD/HLD generate buttons disappear once a version exists; refine needs new
# feedback typed). Small enough never to annoy, large enough to be reliable.
DUPLICATE_WINDOW_S = 3.0

# A "running" marker older than this can only be a leftover from a crashed /
# killed process (finish() runs in a finally) — never block the user forever.
STALE_RUNNING_S = 30 * 60.0

# Upper bound for a server-requested cooldown we will enforce in the UI.
MAX_COOLDOWN_S = 15 * 60.0


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str | None = None   # "running" | "duplicate" | "cooldown"
    message: str | None = None  # safe, user-facing text when refused


def _record(state: MutableMapping) -> dict:
    rec = state.get(STATE_KEY)
    if not isinstance(rec, dict):
        rec = {"running": None, "started": None, "finished": {}, "cooldown_until": None}
        state[STATE_KEY] = rec
    rec.setdefault("finished", {})
    return rec


def is_running(state: MutableMapping, *, clock: Callable[[], float] = time.monotonic) -> bool:
    rec = state.get(STATE_KEY)
    if not isinstance(rec, dict) or rec.get("running") is None:
        return False
    started = rec.get("started")
    return not (isinstance(started, (int, float)) and clock() - started > STALE_RUNNING_S)


def cooldown_remaining(state: MutableMapping, *, clock: Callable[[], float] = time.monotonic) -> float:
    rec = state.get(STATE_KEY)
    until = rec.get("cooldown_until") if isinstance(rec, dict) else None
    if not isinstance(until, (int, float)):
        return 0.0
    return max(0.0, until - clock())


def try_begin(
    state: MutableMapping, op: str, *, clock: Callable[[], float] = time.monotonic,
) -> GuardDecision:
    """Mark ``op`` as running in this session, or explain why it must not start."""
    now = clock()
    rec = _record(state)

    if is_running(state, clock=clock):
        return GuardDecision(
            False, "running",
            "A generation is already in progress for this session. Please wait for "
            "it to finish.",
        )

    remaining = cooldown_remaining(state, clock=clock)
    if remaining > 0:
        seconds = max(1, math.ceil(remaining))
        return GuardDecision(
            False, "cooldown",
            "Gemini's free-tier request limit was reached. Please wait about "
            f"{seconds} seconds before trying again.",
        )

    last = rec["finished"].get(op)
    if isinstance(last, (int, float)) and 0 <= now - last < DUPLICATE_WINDOW_S:
        return GuardDecision(
            False, "duplicate",
            "That request was just processed, so the repeated click was ignored.",
        )

    rec["running"] = op
    rec["started"] = now
    return GuardDecision(True)


def finish(
    state: MutableMapping, op: str, *, clock: Callable[[], float] = time.monotonic,
) -> None:
    """Clear the in-progress flag and remember when ``op`` finished. Never raises."""
    try:
        rec = _record(state)
        if rec.get("running") in (op, None):
            rec["running"] = None
            rec["started"] = None
        rec["finished"][op] = clock()
    except Exception:  # pragma: no cover - must never mask the real outcome
        pass


@contextmanager
def slot(
    state: MutableMapping, op: str, *, clock: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    """Run a block that was admitted by ``try_begin``; ALWAYS releases it."""
    try:
        yield
    finally:
        finish(state, op, clock=clock)


def note_retry_after(
    state: MutableMapping, seconds: float | None, *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Record a server-requested wait so this session does not re-send a request
    before Google's retry window has passed. Never raises."""
    try:
        if seconds is None or not (seconds > 0):
            return
        rec = _record(state)
        until = clock() + min(float(seconds), MAX_COOLDOWN_S)
        current = rec.get("cooldown_until")
        if not isinstance(current, (int, float)) or until > current:
            rec["cooldown_until"] = until
    except Exception:  # pragma: no cover
        pass
