"""
Phase 15B — framework-independent pipeline progress infrastructure.

Pure, deterministic, stdlib-only (plus a read-only import of the authoritative
``next_step`` vocabulary from ``app.orchestration.status``). This module has NO
Streamlit import, NO Gemini call, NO persistence, and adds NO dependency.

It provides three things:

  * ``PipelineEvent`` + ``PipelineProgress`` — a *structured* progress channel.
    ``app.orchestration.graph.run_step(on_event=...)`` calls the optional
    callback with ``PipelineEvent`` objects (never log strings, never provider
    exception text). ``PipelineProgress`` is a ready-made, thread-safe,
    in-memory collector for that callback — nothing is stored on disk.

  * ``pipeline_stage_model(status, run_records=None)`` — a pure function that
    turns a read-only ``sdlc_status()`` snapshot (optionally overlaid with the
    records a ``PipelineProgress`` collected during a live run) into a per-step
    state list using the Phase 15 vocabulary
    (PENDING / RUNNING / COMPLETED / WAITING_FOR_APPROVAL / FAILED / BLOCKED /
    READONLY). It NEVER mutates its inputs and it does NOT replace
    ``app/ui/streamlit_app.py::_pipeline_steps`` — Phase 15C wires the UI.

  * ``format_elapsed(...)`` — a pure elapsed-duration formatter.

Stage identifiers (``brd``, ``hld``, ``user_stories``, ``lld``,
``user_story_refinement``, ``test_cases``, ``closure_report``) are UNCHANGED —
they mirror the ``stage=`` values already emitted by ``app.utils.metrics``
telemetry and ``app.orchestration.graph._stage_from_exception``. This module is
simply the first public home for them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

# Read-only reuse of the authoritative next-step vocabulary (NOT redefined here).
from app.orchestration.status import (
    NEXT_APPROVE_BRD,
    NEXT_APPROVE_CLOSURE_REPORT,
    NEXT_APPROVE_HLD,
    NEXT_APPROVE_LLD,
    NEXT_APPROVE_TEST_CASES,
    NEXT_GENERATE_BRD,
    NEXT_GENERATE_CLOSURE_REPORT,
    NEXT_GENERATE_HLD,
    NEXT_GENERATE_LLD,
    NEXT_GENERATE_TEST_CASES,
    NEXT_REVIEW_CLOSURE_REPORT,
)

# --- stage identifiers (UNCHANGED; the public home for the ids that
#     app.utils.metrics `stage=` and graph._stage_from_exception already use) ---
STAGE_BRD = "brd"
STAGE_HLD = "hld"
STAGE_USER_STORIES = "user_stories"
STAGE_LLD = "lld"
STAGE_USER_STORY_REFINEMENT = "user_story_refinement"
STAGE_TEST_CASES = "test_cases"
STAGE_CLOSURE_REPORT = "closure_report"

#: The six stages the orchestration graph actually runs, in pipeline order.
GRAPH_STAGES: tuple[str, ...] = (
    STAGE_BRD,
    STAGE_HLD,
    STAGE_USER_STORIES,
    STAGE_LLD,
    STAGE_TEST_CASES,
    STAGE_CLOSURE_REPORT,
)

#: All seven stage ids -> user-facing labels. Internal ids are never changed.
STAGE_LABELS: dict[str, str] = {
    STAGE_BRD: "Business Requirements (BRD)",
    STAGE_HLD: "High-Level Design (HLD)",
    STAGE_USER_STORIES: "User Stories",
    STAGE_LLD: "Low-Level Design (LLD)",
    STAGE_USER_STORY_REFINEMENT: "User Story Refinement",
    STAGE_TEST_CASES: "Test Cases",
    STAGE_CLOSURE_REPORT: "Closure Report",
}


def stage_label(stage: str | None) -> str:
    """User-facing label for a stage id (identity fallback for unknown ids)."""
    return STAGE_LABELS.get(stage or "", stage or "")


# --- lifecycle states (the Phase 15 vocabulary) --------------------------------
PENDING = "PENDING"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
FAILED = "FAILED"
BLOCKED = "BLOCKED"
READONLY = "READONLY"

# --- event phases / outcomes -------------------------------------------------
PHASE_RUN_STARTED = "run_started"
PHASE_RUN_COMPLETED = "run_completed"
PHASE_RUN_FAILED = "run_failed"
PHASE_STAGE_STARTED = "stage_started"
PHASE_STAGE_COMPLETED = "stage_completed"
PHASE_STAGE_FAILED = "stage_failed"

OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"


@dataclass(frozen=True)
class PipelineEvent:
    """One structured progress event.

    BOUNDED FIELDS ONLY. This record must never carry a prompt, model output,
    artifact content, API key, or a provider/exception *message* — only the
    fields below. ``run_step`` populates it; a caller-supplied ``on_event``
    callback receives it.
    """

    phase: str
    run_id: str = "-"
    stage: str | None = None
    seq: int = 0                       # monotonic order, assigned by PipelineProgress
    elapsed_ms: int | None = None      # stage- or run-elapsed at emit time
    outcome: str | None = None         # "success" | "error" | None
    attempt: int | None = None         # when known (structured-retry stages)
    # run-level summary — set only on PHASE_RUN_COMPLETED:
    pipeline_status: str | None = None  # SDLCState "status": awaiting_approval | complete
    awaiting: str | None = None         # SDLCState "awaiting" gate id, or None
    produced: dict[str, int] | None = None  # artifacts created this run, e.g. {"hld": 1}
    # set only on PHASE_RUN_FAILED — the stage that raised (NEVER the message):
    failure_stage: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """A plain dict copy (``produced`` is defensively copied)."""
        return {
            "phase": self.phase,
            "run_id": self.run_id,
            "stage": self.stage,
            "seq": self.seq,
            "elapsed_ms": self.elapsed_ms,
            "outcome": self.outcome,
            "attempt": self.attempt,
            "pipeline_status": self.pipeline_status,
            "awaiting": self.awaiting,
            "produced": dict(self.produced) if self.produced is not None else None,
            "failure_stage": self.failure_stage,
        }


OnEvent = Callable[[PipelineEvent], None]


class PipelineProgress:
    """Thread-safe, in-memory collector for ``run_step(on_event=...)``.

    Not persisted anywhere. A caller (Phase 15C: the Streamlit session) creates
    one, passes ``progress`` (or ``progress.record``) as ``on_event``, then reads
    ``progress.events`` / ``progress.as_run_records()`` after ``run_step``
    returns. Callable, so ``on_event=progress`` works directly.

    Recording never raises: a malformed event is dropped rather than propagated
    (observability must never break a pipeline run).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[PipelineEvent] = []

    def record(self, event: PipelineEvent | Mapping[str, Any]) -> None:
        try:
            ev = event if isinstance(event, PipelineEvent) else PipelineEvent(**dict(event))
        except Exception:
            return
        with self._lock:
            self._events.append(replace(ev, seq=len(self._events)))

    __call__ = record

    @property
    def events(self) -> list[PipelineEvent]:
        with self._lock:
            return list(self._events)

    @property
    def run_id(self) -> str:
        for ev in self.events:
            if ev.run_id and ev.run_id != "-":
                return ev.run_id
        return "-"

    def as_run_records(self) -> dict[str, Any]:
        """Fold the collected events into the overlay ``pipeline_stage_model``
        consumes: ``{"run_id", "stages": {stage: {...}}, "failure_stage",
        "pipeline_status", "awaiting", "produced"}``. Pure; safe to call anytime.
        """
        stages: dict[str, dict[str, Any]] = {}
        failure_stage: str | None = None
        pipeline_status: str | None = None
        awaiting: str | None = None
        produced: dict[str, int] | None = None

        for ev in self.events:
            if ev.stage and ev.phase in (
                PHASE_STAGE_STARTED, PHASE_STAGE_COMPLETED, PHASE_STAGE_FAILED
            ):
                rec = stages.setdefault(ev.stage, {})
                if ev.phase == PHASE_STAGE_STARTED:
                    rec.update(state=RUNNING, elapsed_ms=None)
                elif ev.phase == PHASE_STAGE_COMPLETED:
                    rec.update(state=COMPLETED, elapsed_ms=ev.elapsed_ms)
                else:  # PHASE_STAGE_FAILED
                    rec.update(state=FAILED, elapsed_ms=ev.elapsed_ms)
                    failure_stage = ev.stage
                if ev.attempt is not None:
                    rec["attempts"] = ev.attempt
                rec.setdefault("attempts", None)
            elif ev.phase == PHASE_RUN_FAILED:
                failure_stage = ev.failure_stage or failure_stage
            elif ev.phase == PHASE_RUN_COMPLETED:
                pipeline_status = ev.pipeline_status
                awaiting = ev.awaiting
                produced = dict(ev.produced) if ev.produced is not None else {}

        return {
            "run_id": self.run_id,
            "stages": stages,
            "failure_stage": failure_stage,
            "pipeline_status": pipeline_status,
            "awaiting": awaiting,
            "produced": produced,
        }


# --- stage-state model ------------------------------------------------------
#
# Nine step rows, mirroring app/ui/streamlit_app.py::_RAIL_STEPS. Kept here as
# this module's own copy so the infrastructure stays UI-independent; Phase 15C
# reconciles the two into a single source. Step->stage: steps 1 & 2 are the BRD
# (generate / approve); step 8 (Traceability) has no generation stage.
_MODEL_STEPS: tuple[tuple[int, str, str | None], ...] = (
    (1, "SOW → BRD", STAGE_BRD),
    (2, "BRD Workspace", STAGE_BRD),
    (3, "HLD Workspace", STAGE_HLD),
    (4, "User Story Workspace", STAGE_USER_STORIES),
    (5, "LLD Workspace", STAGE_LLD),
    (6, "User Story Refinement", STAGE_USER_STORY_REFINEMENT),
    (7, "QA / Test Case Workspace", STAGE_TEST_CASES),
    (8, "Traceability & Quality", None),
    (9, "Closure Report", STAGE_CLOSURE_REPORT),
)

# next_step value -> the rail step it points at. `generate_user_stories` is
# tolerated (sdlc_status() never emits it today — user stories have no gate) so a
# caller/test can still drive it.
_NEXT_TO_STEP: dict[str, int] = {
    NEXT_GENERATE_BRD: 1,
    NEXT_APPROVE_BRD: 2,
    NEXT_GENERATE_HLD: 3,
    NEXT_APPROVE_HLD: 3,
    "generate_user_stories": 4,
    NEXT_GENERATE_LLD: 5,
    NEXT_APPROVE_LLD: 5,
    NEXT_GENERATE_TEST_CASES: 7,
    NEXT_APPROVE_TEST_CASES: 7,
    NEXT_GENERATE_CLOSURE_REPORT: 9,
    NEXT_APPROVE_CLOSURE_REPORT: 9,
    NEXT_REVIEW_CLOSURE_REPORT: 9,
}

_STAGE_TO_STEPS: dict[str, list[int]] = {}
for _n, _lbl, _stg in _MODEL_STEPS:
    if _stg is not None:
        _STAGE_TO_STEPS.setdefault(_stg, []).append(_n)


def _min_step_for_stage(stage: str | None) -> int | None:
    steps = _STAGE_TO_STEPS.get(stage or "")
    return min(steps) if steps else None


def _base_state(num: int, status: Mapping[str, Any]) -> str:
    """Persisted-status-only state for one rail step (no run overlay).

    Uses the same fields ``app/ui/streamlit_app.py::_pipeline_steps`` reads:
    the ``*_final_version`` pointers and the ``awaiting_*_approval`` booleans
    that ``sdlc_status()`` always returns.
    """
    g = status.get
    if num == 1:
        return COMPLETED if g("brd_exists") else PENDING
    if num == 2:
        if g("brd_final_version") is not None:
            return COMPLETED
        return WAITING_FOR_APPROVAL if g("awaiting_brd_approval") else PENDING
    if num == 3:
        if g("hld_final_version") is not None:
            return COMPLETED
        return WAITING_FOR_APPROVAL if g("awaiting_hld_approval") else PENDING
    if num == 4:
        return COMPLETED if g("us_exists") else PENDING
    if num == 5:
        if g("lld_final_version") is not None:
            return COMPLETED
        return WAITING_FOR_APPROVAL if g("awaiting_lld_approval") else PENDING
    if num == 6:
        return READONLY if g("us_exists") else PENDING
    if num == 7:
        if g("tc_final_version") is not None:
            return COMPLETED
        return WAITING_FOR_APPROVAL if g("awaiting_test_cases_approval") else PENDING
    if num == 8:
        return READONLY if g("brd_exists") else PENDING
    if num == 9:
        stale = bool(g("closure_report_stale"))
        if g("closure_final_version") is not None and not stale:
            return COMPLETED
        if g("closure_exists") and (stale or g("awaiting_closure_approval")):
            return WAITING_FOR_APPROVAL
        return PENDING
    return PENDING


def pipeline_stage_model(
    status: Mapping[str, Any],
    run_records: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pure: an ``sdlc_status()`` snapshot (+ optional live-run overlay) -> the
    nine rail-step descriptors in the Phase 15 vocabulary.

    Each row: ``{step, stage, label, state, is_next, is_failure, elapsed_ms,
    attempts}``.

    * ``run_records is None`` -> persisted-status semantics only:
        - ``next_step`` ``generate_*`` -> the target step is ``PENDING``;
        - ``next_step`` ``approve_*`` / ``review_closure_report`` -> the target
          step is ``WAITING_FOR_APPROVAL``;
        - finalized / generated artifacts -> ``COMPLETED``;
        - steps 6 (US Refinement) and 8 (Traceability) -> ``READONLY`` once their
          prerequisite exists, else ``PENDING``.
    * ``run_records`` supplied (from ``PipelineProgress.as_run_records()``):
        - a stage recorded ``COMPLETED`` -> ``COMPLETED`` (kept as
          ``WAITING_FOR_APPROVAL`` if the persisted status still needs approval),
          with its ``elapsed_ms`` / ``attempts``;
        - the active stage -> ``RUNNING``;
        - the failed stage -> ``FAILED``;
        - every later generation/approval step -> ``BLOCKED`` (``READONLY`` steps
          stay ``READONLY``).

    Never mutates ``status`` or ``run_records``.
    """
    next_step = status.get("next_step")
    next_step_num = _NEXT_TO_STEP.get(next_step) if next_step is not None else None

    records = run_records or {}
    run_stages: Mapping[str, Any] = records.get("stages") or {}
    failure_stage = records.get("failure_stage")
    failed_step_num = _min_step_for_stage(failure_stage) if failure_stage else None

    rows: list[dict[str, Any]] = []
    for num, label, stage in _MODEL_STEPS:
        state = _base_state(num, status)
        elapsed_ms: int | None = None
        attempts: int | None = None

        rec = run_stages.get(stage) if stage else None
        if rec:
            elapsed_ms = rec.get("elapsed_ms")
            attempts = rec.get("attempts")
            rec_state = rec.get("state")
            if rec_state == FAILED:
                state = FAILED
            elif rec_state == RUNNING:
                state = RUNNING
            elif rec_state == COMPLETED:
                state = state if state == WAITING_FOR_APPROVAL else COMPLETED

        if (
            failed_step_num is not None
            and num > failed_step_num
            and state not in (COMPLETED, RUNNING, FAILED, READONLY, WAITING_FOR_APPROVAL)
        ):
            state = BLOCKED

        rows.append(
            {
                "step": num,
                "stage": stage,
                "label": label,
                "state": state,
                "is_next": (num == next_step_num) and failure_stage is None,
                "is_failure": stage is not None and stage == failure_stage,
                "elapsed_ms": elapsed_ms,
                "attempts": attempts,
            }
        )
    return rows


# --- elapsed formatting ---------------------------------------------------

def format_elapsed(seconds: float | int | None) -> str:
    """Human elapsed duration.

    ``None`` -> ``"—"``; ``0`` -> ``"0s"``; ``1.4`` -> ``"1.4s"``;
    ``59.9`` -> ``"59.9s"``; ``60`` -> ``"1m 00s"``; ``123`` -> ``"2m 03s"``.
    """
    if seconds is None:
        return "—"
    s = max(0.0, float(seconds))
    if s < 60:
        return "0s" if s == 0.0 else f"{s:.1f}s"
    total = int(round(s))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs:02d}s"


def format_elapsed_ms(milliseconds: float | int | None) -> str:
    """``format_elapsed`` for a millisecond value (``None`` -> ``"—"``)."""
    if milliseconds is None:
        return "—"
    return format_elapsed(float(milliseconds) / 1000.0)
