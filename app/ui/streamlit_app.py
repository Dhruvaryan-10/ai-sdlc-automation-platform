"""
Streamlit UI for Phase 1: SOW -> BRD Business Analyst Agent.

DESIGN NOTE (why this file stays "dumb"):
This module renders widgets and nothing else. Every piece of real work —
parsing, Gemini calls, version creation, lock management, DOCX export — is
delegated to BusinessAnalystService or the document generator. That keeps the
business logic testable without Streamlit, and means this UI could be swapped
for a real web frontend without touching any of the logic beneath it.

ERROR HANDLING POLICY:
Normal users must never see a Python traceback. Every action is wrapped in a
handler that maps known exception types to plain-English messages, logs the
full technical detail, and falls back to a generic message for anything
unexpected. API keys and secrets are never rendered or logged.

Run with:  streamlit run app/ui/streamlit_app.py
"""

import re
import sys
import time
import uuid
from pathlib import Path

import streamlit as st

# Allow running as `streamlit run app/ui/streamlit_app.py` from the project root.
sys.path.append(str(Path(__file__).resolve().parents[2]))

# --- Phase 13B: configuration boundary ---------------------------------------
# `app/utils/config.py` runs `Settings()` at import time; the first `app.*`
# import below triggers it transitively (agent -> llm -> config). Load and
# validate it HERE, in isolation — `config.py` imports only `pathlib` +
# `pydantic_settings` (no logger, no other `app` module, so no import cycle) —
# so an invalid `.env` renders a clean message instead of an unhandled
# traceback from deep in the import chain. ONLY a configuration
# `ValidationError` is handled here; anything else propagates normally.
from pydantic import ValidationError as _ConfigValidationError

try:
    from app.utils.config import settings
except _ConfigValidationError:
    st.error(
        "**Configuration error — the application cannot start.**\n\n"
        "One or more settings in your `.env` file are missing or invalid. Check that:\n\n"
        "- `GOOGLE_API_KEY` is set to a real Gemini API key\n"
        "- `GEMINI_TEMPERATURE` is between `0.0` and `2.0`\n"
        "- `GEMINI_TIMEOUT_SECONDS` is `1` or greater\n"
        "- `GEMINI_MAX_RETRIES` is `0` or greater\n"
        "- `LOG_LEVEL` is one of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`\n\n"
        "See **README section 2.3** for setup. The specific value is not shown here "
        "for security; check the application logs for the field name."
    )
    st.stop()

# Phase 12B: exception-type imports that previously fed `friendly_error`'s
# isinstance ladder now live in `app.utils.errors` (the classifier). The UI only
# needs the service classes + ProjectMetadata; every error path goes through
# `friendly_error` -> `app.utils.errors.classify`.
from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.user_story_refinement.service import UserStoryRefinementService
from app.agents.test_case.service import TestCaseService
from app.agents.closure_report.service import ClosureReportService
from app.orchestration.graph import run_step
from app.orchestration.status import sdlc_status
from app.quality.project_quality_report import build_project_reports_for_project
from app.document_generator.brd_generator import (
    generate_brd_docx,
    generate_closure_report_docx,
    generate_hld_docx,
    generate_lld_docx,
    generate_test_cases_docx,
    generate_user_stories_docx,
)
from app.observability import pipeline_progress as _pp
from app.observability.pipeline_progress import (
    PipelineProgress,
    format_elapsed_ms,
    pipeline_stage_model,
    stage_label,
)
from app.ui.project_registry import list_existing_projects, sanitize_project_id
from app.utils.errors import classify, log_app_error
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Phase 13B: one sanitized startup line — the effective configuration a session
# is running with (never the API key; `run_id` is "-" outside a pipeline run).
logger.info(
    "config: %s",
    " ".join(f"{k}={v}" for k, v in settings.summary_for_log().items()),
)

st.set_page_config(page_title="BA Agent - SOW to BRD", layout="wide")


# --- human-readable labels ------------------------------------------------------

SOURCE_LABELS = {
    "initial": "Initial Generation",
    "manual_edit": "Manual Edit",
    "ai_refine": "AI Refinement",
}


def switch_project(pid: str) -> None:
    """Wipe the session and reopen the app on project `pid`.

    Uses the same safe session_state wipe the "Start New Project" button has
    always used, then records the chosen id in both session_state and the URL
    (so a browser refresh reopens the same project) and reruns so every service
    and cached version list is rebuilt against `pid`.
    """
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.session_state.project_id = pid
    st.query_params["project"] = pid
    st.rerun()


# A metadata line: "**Key:** value" (key may contain spaces / "/" but no "*",
# and there must be a real value after the closing "**"). Label-only lines such
# as "**Preconditions:**" / "**Expected Result:**" deliberately do NOT match —
# they are already separated by the list / prose that follows them.
_METADATA_LINE_RE = re.compile(r"^\s*\*\*[^*\n]+:\*\*\s+\S")

# A legacy bullet-wrapped numbered step: "- 1. text" / "  - 2) text". Test Case
# versions persisted before canonical step numbering stored steps this way;
# CommonMark then renders a bullet AND a number ("• 1. text").
_LEGACY_NUMBERED_BULLET_RE = re.compile(r"^(\s*)-\s+(\d+)[.)]\s+")


def _flatten_legacy_numbered_bullets(content: str) -> str:
    """Display-only: rewrite legacy "- N. text" step lines to "N. text".

    Test Case documents generated before the renderer emitted a real ordered
    list stored steps as bullet items that themselves start with "1." / "2)" —
    CommonMark reads that as a bullet containing an ordered list and paints
    "• 1. text". Dropping the leading "- " (and normalising ")" to ".") makes it
    render as a plain ordered list while keeping the stored numbers.

    Never touches the stored artifact. Ordinary bullets ("- Preconditions text")
    have no leading number and are left alone; current-format lines ("1. text")
    have no leading "- " and are left alone. Lines inside fenced code blocks are
    skipped. Idempotent.
    """
    if not content:
        return content or ""

    lines = content.split("\n")
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        lines[i] = _LEGACY_NUMBERED_BULLET_RE.sub(r"\1\2. ", line, count=1)
    return "\n".join(lines)


def _hard_break_metadata_lines(content: str) -> str:
    """Display-only: give each "**Key:** value" line a Markdown hard line break.

    Streamlit's st.markdown (CommonMark) folds a run of consecutive non-blank
    lines into ONE paragraph, rendering the newlines as spaces — so the
    per-test-case metadata block ("Requirement / User Story Reference: ... BRD
    Reference: ... Priority: ...") and the document header block run together on
    one line. Appending two trailing spaces turns the soft break into a hard
    break so each field keeps its own line. Long HLD/LLD references then wrap
    naturally within the viewport.

    This never touches the stored artifact — it is applied only to the string
    handed to st.markdown for preview. It does not add blank lines, skips lines
    inside fenced code blocks, skips label-only lines, and is idempotent.
    """
    if not content:
        return content or ""

    lines = content.split("\n")
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not _METADATA_LINE_RE.match(line):
            continue
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        if next_line.strip() == "" or line.endswith("  "):
            continue  # last line of a run needs no break; don't double up
        lines[i] = line + "  "
    return "\n".join(lines)


def render_artifact_markdown(content: str) -> None:
    """Render a stored artifact document in a preview pane.

    Three display-only fixes, none of which changes the stored Markdown or the
    DOCX export. Applied in order:

    1. Legacy "- N. text" step lines -> "N. text" so they render as a plain
       ordered list instead of "• 1. text" (see _flatten_legacy_numbered_bullets).
    2. Consecutive "**Key:** value" metadata lines get Markdown hard line breaks
       so st.markdown does not fold them into one paragraph
       (see _hard_break_metadata_lines).
    3. A literal "$" is escaped to "\\$" — st.markdown treats "$...$" as LaTeX,
       which mangles currency values (e.g. "$29.99 and B2B price $19.99").
    """
    prepared = _flatten_legacy_numbered_bullets(content or "")
    prepared = _hard_break_metadata_lines(prepared)
    prepared = prepared.replace("$", "\\$")
    st.markdown(prepared)


def story_version_label(v) -> str:
    """Human label for a user-story version.

    A Phase 5 artifact-refinement version has source == "ai_refine" AND a
    composite source_ref (contains ';'); the Phase 3 freeform "AI Refine" path
    leaves source_ref None, so the two are distinguishable in the workspace.
    """
    if v.source == "ai_refine" and v.source_ref and ";" in v.source_ref:
        return "Artifact Refinement"
    return SOURCE_LABELS.get(v.source, v.source)


def friendly_error(exc: Exception) -> str:
    """Classify `exc` (`app.utils.errors.classify`) and return a safe user-facing
    message. Technical detail is logged at the classification's level via
    `log_app_error`; no traceback, provider text, `ValidationError` values, API
    keys, or artifact content ever reaches the UI. Every existing caller —
    `st.error(friendly_error(exc))` — is unchanged (Phase 12B)."""
    classified = classify(exc)
    log_app_error(logger, classified, exc)
    return classified.user_message


# --- Phase 8B-6: pure SDLC Pipeline panel formatting helpers -----------------------
#
# These convert an `app.orchestration.status.sdlc_status()` dict into display text
# ONLY - no `st.*` calls, no side effects, nothing invented beyond the dict's own
# fields (User Stories intentionally has no final-version concept: sdlc_status()
# does not expose one, because no approval gate is modeled for user stories).

_NEXT_STEP_LABELS = {
    "generate_brd": "Next: Generate the BRD",
    "approve_brd": "Next: Review and approve the BRD in Step 2",
    "generate_hld": "Next: Generate the HLD",
    "approve_hld": "Next: Review and approve the HLD in Step 3",
    "generate_lld": "Next: Generate the LLD",
    "approve_lld": "Next: Review and approve the LLD in Step 5",
    "generate_test_cases": "Next: Generate Test Cases in Step 7",
    "approve_test_cases": "Next: Review and approve Test Cases in Step 7",
    "generate_closure_report": "Next: Generate the Closure Report in Step 9",
    "approve_closure_report": "Next: Review and approve the Closure Report in Step 9",
    "review_closure_report": (
        "Next: Review the Closure Report in Step 9 — its evidence changed since it was finalized"
    ),
    None: "SDLC pipeline complete — no further orchestrated action is required.",
}

_AWAITING_APPROVAL_MESSAGES = {
    "approve_brd": "Waiting for BRD approval — review and choose the final BRD in Step 2.",
    "approve_hld": "Waiting for HLD approval — review and choose the final HLD in Step 3.",
    "approve_lld": "Waiting for LLD approval — review and choose the final LLD in Step 5.",
    "approve_test_cases": (
        "Waiting for Test Case approval — review and choose the final Test Cases in Step 7."
    ),
    "approve_closure_report": (
        "Waiting for Closure Report approval — review and choose the final Closure "
        "Report in Step 9."
    ),
}

# --- SDLC step rail (Phase 8B-8; renumbered to the 9-step lifecycle in 10B) -------
#
# A horizontally-scrollable progress rail for the nine SDLC steps, rendered in the
# SDLC Pipeline panel. It is a display component only: no navigation/routing, no
# backend calls. State per step is derived entirely from `sdlc_status()` (see
# `_pipeline_steps`). The nine rail steps line up 1:1 with the nine Step tabs
# below. All CSS is scoped under `.sdlc-steprail-wrap` so it cannot affect the
# sidebar, tabs, buttons, artifact cards, or any other component.

_RAIL_STEPS = (
    (1, "SOW → BRD"),
    (2, "BRD Workspace"),
    (3, "HLD Workspace"),
    (4, "User Story Workspace"),
    (5, "LLD Workspace"),
    (6, "User Story Refinement"),
    (7, "QA / Test Case Workspace"),
    (8, "Traceability & Quality"),
    (9, "Closure Report"),
)

# Which rail step the pipeline's current `next_step` points at (generate/approve
# for one artifact map to the same rail card). Steps 6 (User Story Refinement)
# and 8 (Traceability & Quality) are not orchestrated `next_step` targets.
_NEXT_STEP_TO_RAIL = {
    "generate_brd": 1, "approve_brd": 2,
    "generate_hld": 3, "approve_hld": 3,
    "generate_lld": 5, "approve_lld": 5,
    "generate_test_cases": 7, "approve_test_cases": 7,
    "generate_closure_report": 9, "approve_closure_report": 9,
    "review_closure_report": 9,
}

_STEP_RAIL_CSS = """
<style>
.sdlc-steprail-wrap { margin: 0.25rem 0 0.35rem 0; }
.sdlc-steprail-wrap .sdlc-steprail {
    display: flex; flex-direction: row; flex-wrap: nowrap;
    gap: 0.55rem; align-items: flex-start;
    padding: 0.85rem 0.9rem 0.7rem 0.9rem;
    border: 1px solid rgba(250, 250, 250, 0.14);
    border-radius: 12px;
    background: rgba(250, 250, 250, 0.02);
    overflow-x: auto; overflow-y: hidden;
    scrollbar-width: thin;
    scrollbar-color: rgba(250, 250, 250, 0.32) rgba(250, 250, 250, 0.06);
}
.sdlc-steprail-wrap .sdlc-steprail::-webkit-scrollbar { height: 8px; }
.sdlc-steprail-wrap .sdlc-steprail::-webkit-scrollbar-track {
    background: rgba(250, 250, 250, 0.06); border-radius: 8px;
}
.sdlc-steprail-wrap .sdlc-steprail::-webkit-scrollbar-thumb {
    background: rgba(250, 250, 250, 0.28); border-radius: 8px;
}
.sdlc-steprail-wrap .sdlc-steprail::-webkit-scrollbar-thumb:hover {
    background: rgba(250, 250, 250, 0.42);
}
.sdlc-steprail-wrap .sdlc-step {
    flex: 0 0 auto; min-width: 128px; max-width: 168px;
    display: flex; flex-direction: column; align-items: center;
    text-align: center; gap: 0.3rem;
}
.sdlc-steprail-wrap .sdlc-step-num {
    width: 32px; height: 32px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 0.85rem; line-height: 1;
    border: 2px solid transparent;
}
.sdlc-steprail-wrap .sdlc-step-name {
    font-size: 0.78rem; font-weight: 600; line-height: 1.2;
    color: rgba(250, 250, 250, 0.90);
}
.sdlc-steprail-wrap .sdlc-step-status { font-size: 0.7rem; line-height: 1.15; }
.sdlc-steprail-wrap .sdlc-step--done .sdlc-step-num { background: #15803d; color: #ffffff; }
.sdlc-steprail-wrap .sdlc-step--done .sdlc-step-status { color: #4ade80; }
.sdlc-steprail-wrap .sdlc-step--done .sdlc-step-status::before { content: "\\2713\\00a0"; }
.sdlc-steprail-wrap .sdlc-step--current .sdlc-step-num {
    background: #2563eb; color: #ffffff; box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.30);
}
.sdlc-steprail-wrap .sdlc-step--current .sdlc-step-status { color: #60a5fa; font-weight: 600; }
.sdlc-steprail-wrap .sdlc-step--todo .sdlc-step-num {
    background: rgba(250, 250, 250, 0.08); color: rgba(250, 250, 250, 0.60);
    border-color: rgba(250, 250, 250, 0.18);
}
.sdlc-steprail-wrap .sdlc-step--todo .sdlc-step-status { color: rgba(250, 250, 250, 0.45); }
.sdlc-steprail-wrap .sdlc-step--readonly .sdlc-step-num {
    background: rgba(250, 250, 250, 0.08); color: rgba(250, 250, 250, 0.72);
    border-color: rgba(250, 250, 250, 0.18);
}
.sdlc-steprail-wrap .sdlc-step--readonly .sdlc-step-status { color: rgba(250, 250, 250, 0.55); }
.sdlc-steprail-wrap .sdlc-step--running .sdlc-step-num {
    background: #2563eb; color: #ffffff; box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.30);
}
.sdlc-steprail-wrap .sdlc-step--running .sdlc-step-status { color: #60a5fa; font-weight: 600; }
.sdlc-steprail-wrap .sdlc-step--failed .sdlc-step-num { background: #b91c1c; color: #ffffff; }
.sdlc-steprail-wrap .sdlc-step--failed .sdlc-step-status { color: #f87171; font-weight: 600; }
.sdlc-steprail-wrap .sdlc-step--blocked .sdlc-step-num {
    background: rgba(250, 250, 250, 0.05); color: rgba(250, 250, 250, 0.32);
    border-color: rgba(250, 250, 250, 0.12);
}
.sdlc-steprail-wrap .sdlc-step--blocked .sdlc-step-status { color: rgba(250, 250, 250, 0.32); }
</style>
"""


def _next_step_label(status: dict) -> str:
    """Pure: `sdlc_status()["next_step"]` -> a short human sentence.

    Only the currently known `next_step` values are supported (generate_brd /
    approve_brd / generate_hld / approve_hld / generate_lld / approve_lld /
    generate_test_cases / approve_test_cases / None); an unrecognized value falls
    back to a neutral message rather than raising.
    """
    next_step = status.get("next_step")
    if next_step in _NEXT_STEP_LABELS:
        return _NEXT_STEP_LABELS[next_step]
    return "Status unavailable."


def _awaiting_approval_message(status: dict) -> str | None:
    """Pure: the approval-gate warning text for the current `next_step`, or None
    when no artifact is currently awaiting approval."""
    return _AWAITING_APPROVAL_MESSAGES.get(status.get("next_step"))


def _pipeline_summary(status: dict) -> list[str]:
    """Pure: one compact status line per artifact, using ONLY sdlc_status()'s
    own fields - never internal dict syntax, never an invented field."""

    def _line(label: str, exists: bool, latest, final=None, awaiting=None, *, has_gate: bool = True):
        if not exists:
            return f"{label}: not generated"
        text = f"{label}: v{latest}"
        if has_gate:
            if final is not None:
                text += f" (final: v{final})"
            elif awaiting:
                text += " (awaiting approval)"
            else:
                text += " (draft)"
        return text

    closure_line = _line(
        "Closure Report", status["closure_exists"], status["closure_latest_version"],
        status["closure_final_version"], status["awaiting_closure_approval"],
    )
    if status.get("closure_report_stale") and status["closure_exists"]:
        closure_line += " — evidence may be stale"

    return [
        _line("BRD", status["brd_exists"], status["brd_latest_version"],
              status["brd_final_version"], status["awaiting_brd_approval"]),
        _line("HLD", status["hld_exists"], status["hld_latest_version"],
              status["hld_final_version"], status["awaiting_hld_approval"]),
        _line("User Stories", status["us_exists"], status["us_latest_version"],
              has_gate=False),
        _line("LLD", status["lld_exists"], status["lld_latest_version"],
              status["lld_final_version"], status["awaiting_lld_approval"]),
        _line("Test Cases", status["tc_exists"], status["tc_latest_version"],
              status["tc_final_version"], status["awaiting_test_cases_approval"]),
        closure_line,
    ]


def _rail_state_and_text(n: int, row: dict, status: dict) -> tuple[str, str]:
    """Pure: translate ONE `pipeline_progress.pipeline_stage_model()` row into the
    existing rail `(state, status-text)` pair.

    Backward-compatible rail vocabulary — "done" / "current" / "todo" / "readonly"
    — plus the Phase 15C visual states "running" / "failed" / "blocked" which only
    appear when a live-run overlay (`run_records`) is supplied. All stage/state
    semantics come from `pipeline_stage_model()`; this only picks the display
    words (`status` is read solely for the step-9 stale-evidence phrasing).
    """
    model_state = row.get("state")
    elapsed_ms = row.get("elapsed_ms")
    suffix = f" · {format_elapsed_ms(elapsed_ms)}" if elapsed_ms is not None else ""
    if model_state == _pp.COMPLETED:
        return "done", "Completed" + suffix
    if model_state == _pp.RUNNING:
        return "running", "Running…" + suffix
    if model_state == _pp.FAILED:
        return "failed", "Failed" + suffix
    if model_state == _pp.BLOCKED:
        return "blocked", "Blocked"
    if model_state == _pp.WAITING_FOR_APPROVAL:
        if n == 9 and status.get("closure_exists") and status.get("closure_report_stale"):
            return "current", "Review — evidence may be stale"
        return "current", "Awaiting approval"
    if model_state == _pp.READONLY:
        return "readonly", ("Optional" if n == 6 else "Read-only")
    # PENDING
    if row.get("is_next"):
        return "current", "Current"
    return "todo", "Not started"


def _pipeline_steps(status: dict, run_records: dict | None = None) -> list[dict]:
    """Pure: `sdlc_status()` (+ optional live-run `run_records`) -> the 9 SDLC
    step-rail descriptors.

    Each entry is `{"n": int, "name": str, "state": str, "status": str}`.
    `state` is one of "done" / "current" / "todo" / "readonly" (backward
    compatible) and, when `run_records` is supplied, additionally
    "running" / "failed" / "blocked".

    Phase 15C: the authoritative per-stage semantics come from
    `app.observability.pipeline_progress.pipeline_stage_model()` — this function
    only maps its rows onto the pre-existing rail representation. No Streamlit
    calls; `_RAIL_STEPS` stays the source of the step numbers / labels / order.
    """
    model = {row["step"]: row for row in pipeline_stage_model(status, run_records)}
    steps: list[dict] = []
    for n, name in _RAIL_STEPS:
        state, text = _rail_state_and_text(n, model.get(n, {}), status)
        steps.append({"n": n, "name": name, "state": state, "status": text})
    return steps


def _render_step_rail(status: dict, run_records: dict | None = None) -> str:
    """Pure: build the scoped HTML for the horizontal 9-step SDLC rail.

    Content is built only from `_RAIL_STEPS` (static labels) + `_pipeline_steps`
    (fixed status words) + integer step numbers — no user/project text is
    interpolated, so no escaping is required. No Streamlit calls.
    """
    cells = "".join(
        f'<div class="sdlc-step sdlc-step--{s["state"]}">'
        f'<div class="sdlc-step-num">{s["n"]}</div>'
        f'<div class="sdlc-step-name">{s["name"]}</div>'
        f'<div class="sdlc-step-status">{s["status"]}</div>'
        f'</div>'
        for s in _pipeline_steps(status, run_records)
    )
    return (
        '<div class="sdlc-steprail-wrap">'
        '<div class="sdlc-steprail" role="list" aria-label="SDLC pipeline steps">'
        f'{cells}</div></div>'
    )


def run_pipeline_step(
    project_id: str, ba_service, sa_service, us_service, lld_service, tc_service,
    closure_service=None, *, on_event=None,
):
    """Thin, testable wrapper around `run_step()` for the pipeline panel's button.

    Exists ONLY so tests can monkeypatch/spy on `run_step` without importing and
    executing the whole Streamlit script's button-click branch. Contains no
    business logic of its own - `run_step` remains the single source of truth,
    unmodified. Never called except from the explicit "Run SDLC Pipeline" button
    handler below (never on import, page load, or a plain rerun).

    Phase 15C: `on_event` is forwarded verbatim to `run_step(on_event=...)` (the
    optional Phase 15B structured-progress callback). Omitted -> unchanged.
    """
    return run_step(
        project_id,
        request="ensure_brd",
        ba_service=ba_service,
        sa_service=sa_service,
        us_service=us_service,
        lld_service=lld_service,
        tc_service=tc_service,
        closure_service=closure_service,
        on_event=on_event,
    )


# --- Phase 15C: post-run pipeline progress (session-owned, never persisted) --------
#
# `run_step()` is synchronous, so the callback cannot repaint Streamlit while
# Gemini is blocked. These helpers therefore build a POST-RUN timeline from the
# `PipelineProgress` a run collected, plus the returned `SDLCState`. Only bounded
# fields are kept — no `str(exc)`, no provider text, no prompts, no secrets.

def _capture_pipeline_run(project_id: str, progress, final_state) -> dict:
    """Safe in-memory summary for the session's "pipeline_run" state key.

    `progress` is the run's `PipelineProgress`; `final_state` is the `SDLCState`
    `run_step()` returned (or None on failure). Nothing here is written to disk.
    Pure: only bounded fields, no Streamlit calls, no exception text.
    """
    records = progress.as_run_records()
    used_state = final_state if isinstance(final_state, dict) else {}
    return {
        "project_id": project_id,
        "run_id": records.get("run_id") or "-",
        "run_records": records,                       # drives the rail overlay
        "stages": records.get("stages") or {},
        "failure_stage": records.get("failure_stage"),
        "pipeline_status": used_state.get("status") or records.get("pipeline_status"),
        "awaiting": used_state.get("awaiting") or records.get("awaiting"),
        "produced": dict(used_state.get("produced") or records.get("produced") or {}),
    }


def _pipeline_run_headline(run_info: dict) -> str:
    """Pure: a short, safe one-line summary of the most recent pipeline run."""
    if run_info.get("failure_stage"):
        return f"Pipeline stopped — failed at: {stage_label(run_info['failure_stage'])}"
    if run_info.get("pipeline_status") == "complete":
        return "Pipeline run complete — no orchestrated step remains"
    return "Pipeline paused — a human approval is required to continue"


def _render_pipeline_run_timeline(run_info: dict) -> None:
    """Render the per-stage post-run timeline (uses `st.*`; not a pure helper)."""
    stages = run_info.get("stages") or {}
    _icons = {_pp.COMPLETED: "✅", _pp.RUNNING: "⏳", _pp.FAILED: "❌"}
    for stage in _pp.GRAPH_STAGES:
        rec = stages.get(stage)
        if not rec:
            continue
        state = rec.get("state") or ""
        line = f"{_icons.get(state, '•')} {stage_label(stage)} — {state.title()}"
        if rec.get("elapsed_ms") is not None:
            line += f" · {format_elapsed_ms(rec.get('elapsed_ms'))}"
        if rec.get("attempts"):
            line += f" (attempt {rec['attempts']})"
        st.write(line)
    if run_info.get("failure_stage"):
        st.write(f"**Failed at: {stage_label(run_info['failure_stage'])}**")
    produced = run_info.get("produced") or {}
    if produced:
        st.caption("Created this run: "
                   + ", ".join(f"{stage_label(k)} v{v}" for k, v in produced.items()))
    run_id = run_info.get("run_id")
    if run_id and run_id != "-":
        st.caption(f"Run reference: `{run_id}`")


def _render_pipeline_run_summary(run_info: dict) -> None:
    """Persistent (post-rerun) block for the last pipeline run (uses `st.*`)."""
    headline = _pipeline_run_headline(run_info)
    failed = bool(run_info.get("failure_stage"))
    if failed:
        st.warning(headline)
    else:
        st.caption(f"Last pipeline run: {headline}")
    with st.expander("Pipeline run timeline", expanded=failed):
        _render_pipeline_run_timeline(run_info)


# --- session bootstrapping --------------------------------------------------------

if "project_id" not in st.session_state:
    # Prefer a valid ?project=<id> from the URL so a browser refresh reopens the
    # same persisted project; otherwise fall back to the historic behaviour of
    # minting a fresh short UUID for a brand-new project.
    from_url = sanitize_project_id(st.query_params.get("project", ""))
    st.session_state.project_id = from_url or str(uuid.uuid4())[:8]

# Keep the URL in sync with the active project (no-op when already equal).
st.query_params["project"] = st.session_state.project_id

if "service" not in st.session_state:
    try:
        st.session_state.service = BusinessAnalystService(project_id=st.session_state.project_id)
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

service: BusinessAnalystService = st.session_state.service

if "sa_service" not in st.session_state:
    try:
        st.session_state.sa_service = SolutionArchitectService(
            project_id=st.session_state.project_id, ba_service=service
        )
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

sa_service: SolutionArchitectService = st.session_state.sa_service

if "us_service" not in st.session_state:
    try:
        st.session_state.us_service = InitialUserStoryService(
            project_id=st.session_state.project_id, ba_service=service
        )
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

us_service: InitialUserStoryService = st.session_state.us_service

if "lld_service" not in st.session_state:
    try:
        st.session_state.lld_service = LowLevelDesignService(
            project_id=st.session_state.project_id,
            sa_service=sa_service,
            ba_service=service,
        )
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

lld_service: LowLevelDesignService = st.session_state.lld_service

if "usr_service" not in st.session_state:
    try:
        st.session_state.usr_service = UserStoryRefinementService(
            project_id=st.session_state.project_id
        )
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

usr_service: UserStoryRefinementService = st.session_state.usr_service

if "qa_service" not in st.session_state:
    try:
        st.session_state.qa_service = TestCaseService(project_id=st.session_state.project_id)
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

qa_service: TestCaseService = st.session_state.qa_service

if "closure_service" not in st.session_state:
    try:
        st.session_state.closure_service = ClosureReportService(
            project_id=st.session_state.project_id
        )
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

closure_service: ClosureReportService = st.session_state.closure_service


def refresh_versions() -> None:
    try:
        st.session_state.versions = service.get_all_versions()
    except Exception as exc:
        st.session_state.versions = []
        st.error(friendly_error(exc))


def refresh_hld_versions() -> None:
    try:
        st.session_state.hld_versions = sa_service.get_all_versions()
    except Exception as exc:
        st.session_state.hld_versions = []
        st.error(friendly_error(exc))


def refresh_us_versions() -> None:
    try:
        st.session_state.us_versions = us_service.get_all_versions()
    except Exception as exc:
        st.session_state.us_versions = []
        st.error(friendly_error(exc))


def refresh_lld_versions() -> None:
    try:
        st.session_state.lld_versions = lld_service.get_all_versions()
    except Exception as exc:
        st.session_state.lld_versions = []
        st.error(friendly_error(exc))


def refresh_qa_versions() -> None:
    try:
        st.session_state.qa_versions = qa_service.get_all_versions()
    except Exception as exc:
        st.session_state.qa_versions = []
        st.error(friendly_error(exc))


def refresh_closure_versions() -> None:
    try:
        st.session_state.closure_versions = closure_service.get_all_versions()
    except Exception as exc:
        st.session_state.closure_versions = []
        st.error(friendly_error(exc))


# --- Phase 11A: deterministic, version-keyed caches for the read-only
# Traceability / Quality computations -------------------------------------------
#
# Reconnaissance found these recomputed on EVERY Streamlit rerun (every
# keystroke): the Step 8 report (traceability matrix built twice per rerun) and
# the top-of-page Phase 5 / Phase 6 staleness block. All are pure, deterministic
# functions of the persisted artifact streams. The cache key is a fingerprint of
# every stream's (count, latest version, final version); any generate / edit /
# refine / finalize / unlock changes it (the refresh_*_versions() helpers keep
# session_state current before this code runs), so a cache hit can NEVER show a
# value that is stale relative to what the rest of the page shows.

def _stream_sig(vlist) -> tuple[int, int, int]:
    """(count, latest version, final version-or-0) for one artifact stream."""
    if not vlist:
        return (0, 0, 0)
    return (
        len(vlist),
        vlist[-1].version,
        next((v.version for v in vlist if v.is_final), 0),
    )


def _artifact_fingerprint() -> tuple:
    """Cache key: a fingerprint of all six artifact streams' version state."""
    return (
        _stream_sig(st.session_state.get("versions")),
        _stream_sig(st.session_state.get("hld_versions")),
        _stream_sig(st.session_state.get("us_versions")),
        _stream_sig(st.session_state.get("lld_versions")),
        _stream_sig(st.session_state.get("qa_versions")),
        _stream_sig(st.session_state.get("closure_versions")),
    )


@st.cache_data(show_spinner=False, max_entries=64)
def _cached_project_reports(project_id: str, fingerprint: tuple) -> dict:
    """Traceability + Quality reports, memoized on the artifact-version
    fingerprint. Recomputed only when some stream actually changes. Read-only,
    no Gemini (see `build_project_reports_for_project`)."""
    return build_project_reports_for_project(project_id)


@st.cache_data(show_spinner=False, max_entries=64)
def _cached_closure_staleness(project_id: str, fingerprint: tuple) -> dict:
    """Closure-report provenance / staleness (Step 9 banner), memoized on the
    same fingerprint. `stale_sources()` / `recorded_source_versions()` /
    `current_source_versions()` are pure functions of the persisted streams."""
    cr = ClosureReportService(project_id=project_id)
    try:
        stale = cr.stale_sources()
        recorded = cr.recorded_source_versions() or {}
        current = cr.current_source_versions()
    except Exception:
        stale, recorded, current = [], {}, {}
    return {"stale_sources": stale, "recorded": recorded, "current": current}


@st.cache_data(show_spinner=False, max_entries=64)
def _cached_staleness(project_id: str, fingerprint: tuple) -> dict:
    """Phase 5 (refinement) + Phase 6 (test-case) provenance / staleness for the
    top-of-page state block, memoized on the same fingerprint. Every value is a
    pure function of the persisted BRD / HLD / LLD / User-Story / Test-Case
    streams, so it is invalidated exactly when any of them changes."""
    usr = UserStoryRefinementService(project_id=project_id)
    qa = TestCaseService(project_id=project_id)
    try:
        usr_recorded = usr.recorded_source_versions()
        usr_stale_sources = usr.stale_sources()
    except Exception:
        usr_recorded, usr_stale_sources = None, []
    try:
        qa_recorded = qa.recorded_source_versions()
        qa_current = qa.current_source_versions()
        qa_stale_sources = qa.stale_sources()
    except Exception:
        qa_recorded, qa_current, qa_stale_sources = None, {}, []
    return {
        "usr_recorded": usr_recorded,
        "usr_stale_sources": usr_stale_sources,
        "qa_recorded": qa_recorded,
        "qa_current": qa_current,
        "qa_stale_sources": qa_stale_sources,
    }


if "versions" not in st.session_state:
    refresh_versions()

if "hld_versions" not in st.session_state:
    refresh_hld_versions()

if "us_versions" not in st.session_state:
    refresh_us_versions()

if "lld_versions" not in st.session_state:
    refresh_lld_versions()

if "qa_versions" not in st.session_state:
    refresh_qa_versions()

if "closure_versions" not in st.session_state:
    refresh_closure_versions()

versions = st.session_state.versions
latest_version = versions[-1] if versions else None
final_version = next((v for v in versions if v.is_final), None)
is_locked = bool(final_version and final_version.is_locked)

hld_versions = st.session_state.hld_versions
hld_latest = hld_versions[-1] if hld_versions else None
hld_final = next((v for v in hld_versions if v.is_final), None)
hld_is_locked = bool(hld_final and hld_final.is_locked)

us_versions = st.session_state.us_versions
us_latest = us_versions[-1] if us_versions else None
us_final = next((v for v in us_versions if v.is_final), None)
us_is_locked = bool(us_final and us_final.is_locked)

lld_versions = st.session_state.lld_versions
lld_latest = lld_versions[-1] if lld_versions else None
lld_final = next((v for v in lld_versions if v.is_final), None)
lld_is_locked = bool(lld_final and lld_final.is_locked)

# --- Phase 11A: one artifact-version fingerprint drives both cached read-only
# views (the Phase 5/6 staleness block below and the Step 8 report). Cheap;
# invalidates the caches the instant any stream changes.
_artifact_fp = _artifact_fingerprint()
_staleness = _cached_staleness(st.session_state.project_id, _artifact_fp)

# --- Phase 5 refinement state (reads the SAME user_stories stream, no second store) ---
usr_recorded = _staleness["usr_recorded"]
usr_stale_sources = _staleness["usr_stale_sources"]
usr_is_refined = usr_recorded is not None      # latest user-story version came from artifact refinement
usr_stale = bool(usr_stale_sources)            # a recorded source artifact changed since that refinement

qa_versions = st.session_state.qa_versions
qa_latest = qa_versions[-1] if qa_versions else None
qa_final = next((v for v in qa_versions if v.is_final), None)
qa_is_locked = bool(qa_final and qa_final.is_locked)

closure_versions = st.session_state.closure_versions
closure_latest = closure_versions[-1] if closure_versions else None
closure_final = next((v for v in closure_versions if v.is_final), None)
closure_is_locked = bool(closure_final and closure_final.is_locked)

# --- Phase 6 test-case provenance / per-source staleness (own test_cases stream) ---
qa_recorded = _staleness["qa_recorded"]
qa_current = _staleness["qa_current"]
qa_stale_sources = _staleness["qa_stale_sources"]
qa_stale = bool(qa_stale_sources)


# --- sidebar: project status + version history --------------------------------------

with st.sidebar:
    st.header("Project")
    st.caption(f"Project ID: `{st.session_state.project_id}`")

    with st.expander("Open / switch project"):
        _existing = list_existing_projects(settings.resolved_output_dir())
        if _existing:
            _default_idx = (
                _existing.index(st.session_state.project_id)
                if st.session_state.project_id in _existing
                else 0
            )
            _picked = st.selectbox(
                "Existing projects", _existing, index=_default_idx, key="project_picker"
            )
            if st.button("Open selected", key="open_selected_project"):
                if _picked and _picked != st.session_state.project_id:
                    switch_project(_picked)
        else:
            st.caption("No saved projects found under the outputs folder yet.")

        _typed = st.text_input(
            "Open by ID", key="open_by_id_input",
            placeholder="e.g. d1801c21",
        )
        if st.button("Open by ID", key="open_by_id_btn"):
            _clean = sanitize_project_id(_typed)
            if _clean is None:
                st.warning(
                    "Not a valid project ID. Use letters, digits, '-' or '_' "
                    "(max 64 characters)."
                )
            elif _clean not in list_existing_projects(settings.resolved_output_dir()):
                # Never create a project just because an ID was typed.
                st.warning(
                    f"No saved project '{_clean}'. Use \"Start New Project\" to "
                    "create one — typing an ID here never creates a project."
                )
            elif _clean != st.session_state.project_id:
                switch_project(_clean)
            else:
                st.info(f"Project '{_clean}' is already open.")

    if latest_version:
        st.metric("Current Version", f"v{latest_version.version}")
    if final_version:
        status = "Locked" if final_version.is_locked else "Unlocked"
        st.success(f"Accepted BRD: v{final_version.version}\n\nStatus: {status}")
    else:
        st.info("No final BRD selected yet.")

    st.divider()
    st.subheader("Version History")

    if not versions:
        st.caption("No versions yet. Generate a BRD to get started.")
    else:
        for v in reversed(versions):
            markers = []
            if latest_version and v.version == latest_version.version:
                markers.append("CURRENT")
            if v.is_final:
                markers.append("ACCEPTED")
            marker_text = f"  [{' / '.join(markers)}]" if markers else ""

            with st.expander(f"v{v.version} - {SOURCE_LABELS.get(v.source, v.source)}{marker_text}"):
                st.caption(f"Created: {v.created_at}")
                st.caption(f"Type: {SOURCE_LABELS.get(v.source, v.source)}")
                st.caption(f"Status: {'Accepted' if v.is_final else 'Draft'}")
                if v.note:
                    st.caption(f"Change: {v.note}")
                if st.button("View this version", key=f"view_{v.version}"):
                    st.session_state.viewing_version = v.version
                    st.rerun()

    st.divider()
    st.subheader("HLD")
    if hld_latest:
        st.metric("Current HLD Version", f"v{hld_latest.version}")
        if hld_final:
            hld_status = "Locked" if hld_final.is_locked else "Unlocked"
            st.success(f"Final HLD: v{hld_final.version}\n\nStatus: {hld_status}")
        else:
            st.info("No final HLD selected yet.")
    elif final_version:
        st.caption("Ready to generate. Open the HLD Workspace tab.")
    else:
        st.caption("Accept a BRD first to unlock the HLD stage.")

    st.divider()
    st.subheader("User Stories")
    if us_latest:
        st.metric("Latest User Stories Version", f"v{us_latest.version}")
        st.caption("User stories are not separately finalized — the latest version "
                   "is always used downstream.")
    elif final_version:
        st.caption("Ready to generate. Open the User Story Workspace tab.")
    else:
        st.caption("Accept a BRD first to unlock the User Story stage.")

    st.divider()
    st.subheader("LLD")
    if lld_latest:
        st.metric("Current LLD Version", f"v{lld_latest.version}")
        if lld_final:
            lld_status = "Locked" if lld_final.is_locked else "Unlocked"
            st.success(f"Final LLD: v{lld_final.version}\n\nStatus: {lld_status}")
        else:
            st.info("No final LLD selected yet.")
    elif hld_final:
        st.caption("Ready to generate. Open the LLD Workspace tab.")
    else:
        st.caption("Accept an HLD first to unlock the LLD stage.")

    st.divider()
    st.subheader("User Story Refinement")
    if usr_is_refined and usr_recorded is not None:
        st.metric("Refined Stories Version", f"v{us_latest.version}")
        st.caption(
            "Source artifacts: "
            f"BRD v{usr_recorded['brd']} · "
            f"HLD v{usr_recorded['hld'] if usr_recorded['hld'] is not None else '—'} · "
            f"LLD v{usr_recorded['lld'] if usr_recorded['lld'] is not None else '—'}"
        )
        if usr_stale:
            st.warning(f"STALE — changed since refinement: {', '.join(usr_stale_sources)}")
        else:
            st.success("Up to date with the accepted BRD / HLD / LLD.")
    elif us_latest is not None and final_version is not None:
        st.caption("Ready to refine. Open the User Story Refinement tab.")
    elif final_version is not None:
        st.caption("Generate initial user stories (Step 4) first.")
    else:
        st.caption("Accept a BRD first to unlock refinement.")

    st.divider()
    st.subheader("QA / Test Cases")
    if qa_latest is not None:
        st.metric("Current Test Case Version", f"v{qa_latest.version}")
        if qa_recorded is not None:
            def _v(x):
                return f"v{x}" if x is not None else "—"
            st.caption(
                "Built from: "
                f"BRD v{qa_recorded['brd']} · "
                f"HLD {_v(qa_recorded['hld'])} · "
                f"LLD {_v(qa_recorded['lld'])} · "
                f"US {_v(qa_recorded['us'])}"
            )
        if qa_final:
            qa_status = "Locked" if qa_final.is_locked else "Unlocked"
            st.success(f"Final Test Cases: v{qa_final.version}\n\nStatus: {qa_status}")
        if qa_stale:
            st.warning(f"STALE — changed since generation: {', '.join(qa_stale_sources)}")
        elif not qa_final:
            st.info("No final test cases selected yet.")
    elif final_version is not None:
        st.caption("Ready to generate. Open the QA / Test Case tab.")
    else:
        st.caption("Accept a BRD first to unlock test-case generation.")

    st.divider()
    if st.button("Start New Project"):
        switch_project(str(uuid.uuid4())[:8])


# --- main area ------------------------------------------------------------------------

st.title("Business Analyst Agent - SOW to BRD")
st.caption("Phase 1: Statement of Work to Business Requirement Document")

# --- SDLC Pipeline panel (Phase 8B-6) -----------------------------------------------
#
# Additive orchestration status/action layer over the seven tabs below. Reads
# sdlc_status() fresh on every render as its single source of truth; the "Run SDLC
# Pipeline" button is the ONLY place run_step() is called, and only in direct
# response to that explicit click - never on import, page load, project switch, or
# a plain rerun. Reuses the SAME session-cached service instances the seven tabs
# already use. Never calls choose_final_* / mark_final / unlock_final* - finalization
# stays exclusively in each artifact's own tab below.
with st.container():
    st.subheader("SDLC Pipeline")
    st.caption(
        "This runs the existing orchestration pipeline from the current project "
        "state and stops at the next required human approval. It may generate "
        "multiple draft artifacts in one run."
    )

    try:
        pipeline_status = sdlc_status(
            st.session_state.project_id,
            ba_service=service,
            sa_service=sa_service,
            us_service=us_service,
            lld_service=lld_service,
            tc_service=qa_service,
            closure_service=closure_service,
        )
    except Exception as exc:
        pipeline_status = None
        st.error(friendly_error(exc))

    if pipeline_status is not None:
        # Phase 15C: overlay the most recent pipeline run's per-stage records
        # (COMPLETED / RUNNING / FAILED / BLOCKED + elapsed) onto the rail, but
        # only for the project it belongs to. Never persisted.
        _prev_run = st.session_state.get("pipeline_run")
        _run_overlay = (
            _prev_run.get("run_records")
            if _prev_run and _prev_run.get("project_id") == st.session_state.project_id
            else None
        )

        # Horizontally-scrollable rail of all nine SDLC steps. Display only —
        # per-stage semantics come from
        # `app.observability.pipeline_progress.pipeline_stage_model()`; the
        # `_pipeline_summary()` text helper is retained for API/test stability.
        st.markdown(_STEP_RAIL_CSS, unsafe_allow_html=True)
        st.markdown(
            _render_step_rail(pipeline_status, _run_overlay), unsafe_allow_html=True
        )

        st.write(f"**{_next_step_label(pipeline_status)}**")

        awaiting_message = _awaiting_approval_message(pipeline_status)
        if awaiting_message:
            st.warning(awaiting_message)

        # Phase 10B: non-blocking closure-report staleness. Informational only —
        # the panel never regenerates, re-approves, or unlocks anything.
        if pipeline_status.get("closure_report_stale"):
            stale_srcs = ", ".join(pipeline_status.get("closure_report_stale_sources") or [])
            st.warning(
                f"A Closure Report exists, but its evidence may be **stale**: "
                f"**{stale_srcs}** changed after it was generated. The pipeline "
                f"does not treat this as fresh closure evidence. Review it in "
                f"**Step 9** and regenerate there if appropriate — nothing is "
                f"regenerated or re-approved automatically."
            )

        # Phase 15C: persistent timeline of the last pipeline run for this project.
        if _prev_run and _prev_run.get("project_id") == st.session_state.project_id:
            _render_pipeline_run_summary(_prev_run)

        pipeline_ready = latest_version is not None
        if not pipeline_ready:
            st.info(
                "Upload a SOW and generate the initial BRD in Step 1 first. Once "
                "a BRD exists, use this panel to continue the pipeline through "
                "the remaining steps."
            )

        if st.button(
            "Run SDLC Pipeline", type="primary",
            disabled=not pipeline_ready, key="pipeline_run_btn",
        ):
            # Phase 15C: `run_step()` is synchronous, so this st.status shows a
            # single "running" state while Gemini is blocked; the per-stage
            # timeline is filled in once the call returns (post-run).
            progress = PipelineProgress()
            with st.status(
                "Running the SDLC orchestration pipeline…", expanded=True,
            ) as pipeline_status_box:
                try:
                    final_state = run_pipeline_step(
                        st.session_state.project_id,
                        service, sa_service, us_service, lld_service, qa_service,
                        closure_service,
                        on_event=progress,
                    )
                    st.session_state["pipeline_run"] = _capture_pipeline_run(
                        st.session_state.project_id, progress, final_state
                    )
                    refresh_versions()
                    refresh_hld_versions()
                    refresh_us_versions()
                    refresh_lld_versions()
                    refresh_qa_versions()
                    refresh_closure_versions()
                    _render_pipeline_run_timeline(st.session_state["pipeline_run"])
                    pipeline_status_box.update(
                        label=_pipeline_run_headline(st.session_state["pipeline_run"]),
                        state="complete", expanded=False,
                    )
                    st.rerun()
                except Exception as exc:
                    st.session_state["pipeline_run"] = _capture_pipeline_run(
                        st.session_state.project_id, progress, None
                    )
                    _render_pipeline_run_timeline(st.session_state["pipeline_run"])
                    pipeline_status_box.update(
                        label=_pipeline_run_headline(st.session_state["pipeline_run"]),
                        state="error", expanded=True,
                    )
                    st.error(friendly_error(exc))

st.divider()

(tab_generate, tab_workspace, tab_hld, tab_stories,
 tab_lld, tab_usr, tab_qa, tab_traceability, tab_closure) = st.tabs(
    ["Step 1: Upload & Generate", "Step 2: BRD Workspace", "Step 3: HLD Workspace",
     "Step 4: User Story Workspace", "Step 5: LLD Workspace",
     "Step 6: User Story Refinement", "Step 7: QA / Test Case Workspace",
     "Step 8: Traceability & Quality", "Step 9: Closure Report"]
)


# --- STEP 1: upload + generate ----------------------------------------------------------

with tab_generate:
    if latest_version is not None:
        st.info("A BRD already exists for this project. Open the BRD Workspace tab to "
                "review it, or start a new project from the sidebar to upload a different SOW.")
    else:
        st.subheader("Project Details")
        col1, col2 = st.columns(2)
        with col1:
            project_name = st.text_input("Project Name", placeholder="e.g. Customer Portal Revamp")
            client_name = st.text_input("Client Name", placeholder="e.g. Acme Corp")
        with col2:
            project_type = st.selectbox(
                "Project Type",
                ["Web Application", "Mobile Application", "Data Platform",
                 "API / Integration", "Internal Tool", "Other"],
            )
            industry = st.text_input("Industry", placeholder="e.g. Banking, Retail, Healthcare")

        st.subheader("Upload Statement of Work (SOW)")
        uploaded_file = st.file_uploader("Supported formats: DOCX, PDF, TXT", type=["docx", "pdf", "txt"])

        ready = bool(uploaded_file and project_name and client_name and industry)

        if st.button("Generate BRD", disabled=not ready, type="primary"):
            with st.spinner("Extracting document, cleaning text, and generating your BRD..."):
                try:
                    upload_path = (settings.resolved_upload_dir()
                                   / f"{st.session_state.project_id}_{uploaded_file.name}")
                    upload_path.write_bytes(uploaded_file.getbuffer())
                    logger.info(f"SOW uploaded: '{uploaded_file.name}' -> '{upload_path}'")

                    metadata = ProjectMetadata(
                        project_name=project_name,
                        client_name=client_name,
                        project_type=project_type,
                        industry=industry,
                    )

                    start = time.time()
                    version = service.generate_initial_brd(upload_path, metadata)
                    elapsed = time.time() - start
                    logger.info(f"BRD v{version.version} generated in {elapsed:.1f}s")

                    refresh_versions()
                    st.session_state.viewing_version = version.version
                    st.success(f"BRD Version {version.version} generated in {elapsed:.1f}s.")
                    st.rerun()
                except Exception as exc:
                    st.error(friendly_error(exc))


# --- STEP 2: workspace ---------------------------------------------------------------------

with tab_workspace:
    if latest_version is None:
        st.info("Upload a SOW and generate a BRD first (Step 1).")
    else:
        viewing_number = st.session_state.get("viewing_version", latest_version.version)
        try:
            viewing_version = service.get_version(viewing_number) or latest_version
        except Exception as exc:
            st.error(friendly_error(exc))
            viewing_version = latest_version

        # This specific version is only editable when it's the newest one AND
        # nothing is locked — editing an older version would silently fork history.
        is_current = viewing_version.version == latest_version.version
        editable = is_current and not is_locked

        # --- status banner ---
        status_cols = st.columns([2, 2, 2])
        with status_cols[0]:
            st.metric("Viewing", f"v{viewing_version.version}")
        with status_cols[1]:
            st.metric("Type", SOURCE_LABELS.get(viewing_version.source, viewing_version.source))
        with status_cols[2]:
            st.metric("Status", "Accepted" if viewing_version.is_final else "Draft")

        if is_locked:
            if viewing_version.is_final:
                st.success(f"This is the Accepted BRD (v{viewing_version.version}) and it is locked "
                           "against further changes.")
            else:
                st.warning(f"The Accepted BRD (v{final_version.version}) is locked. "
                           "Unlock it below to make further changes.")
        elif not is_current:
            st.info(f"You are viewing an older version (v{viewing_version.version}). "
                    f"Editing is only available on the current version "
                    f"(v{latest_version.version}).")

        st.divider()

        tab_preview, tab_edit, tab_refine, tab_history = st.tabs(
            ["Preview", "Edit", "AI Refine", "History"]
        )

        # --- PREVIEW: render markdown as a real document ---
        with tab_preview:
            render_artifact_markdown(viewing_version.content)

        # --- EDIT: manual editing, saving creates a new version ---
        with tab_edit:
            if not editable:
                st.info("Editing is disabled for this version. "
                        + ("Unlock the Accepted BRD to continue." if is_locked
                           else "Switch to the current version to edit."))
                st.text_area("BRD content (read-only)", value=viewing_version.content,
                             height=500, disabled=True, key=f"ro_{viewing_version.version}")
            else:
                st.caption("Edit the BRD below. Saving creates a NEW version - "
                           "the current version is never overwritten.")
                edited_text = st.text_area(
                    "BRD content (markdown)",
                    value=viewing_version.content,
                    height=500,
                    key=f"editor_{viewing_version.version}",
                )
                change_note = st.text_input(
                    "Change description (optional)",
                    placeholder="e.g. Corrected stakeholder list",
                    key=f"note_{viewing_version.version}",
                )
                if st.button("Save as New Version", type="primary"):
                    try:
                        new_version = service.save_manual_edit(
                            edited_text, note=change_note.strip() or "Manual edit"
                        )
                        refresh_versions()
                        st.session_state.viewing_version = new_version.version
                        st.success(f"Saved as Version {new_version.version}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

        # --- AI REFINE: current BRD + feedback -> new version ---
        with tab_refine:
            if not editable:
                st.info("AI refinement is disabled for this version. "
                        + ("Unlock the Accepted BRD to continue." if is_locked
                           else "Switch to the current version to refine."))
            else:
                st.caption("Describe your changes in plain English. The AI receives the CURRENT "
                           "BRD plus your feedback - it does not regenerate from the original SOW. "
                           "Unaffected sections are preserved.")
                feedback = st.text_area(
                    "Refinement instruction",
                    placeholder="e.g. Add Multi-Factor Authentication as a functional requirement",
                    key="feedback_input",
                    height=120,
                )
                if st.button("Refine with AI", type="primary", disabled=not feedback.strip()):
                    with st.spinner("Sending the current BRD and your feedback to Gemini..."):
                        try:
                            start = time.time()
                            new_version = service.refine_with_ai(feedback)
                            elapsed = time.time() - start
                            logger.info(f"BRD refined to v{new_version.version} in {elapsed:.1f}s")

                            refresh_versions()
                            st.session_state.viewing_version = new_version.version
                            st.success(f"Created Version {new_version.version} in {elapsed:.1f}s.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))

        # --- HISTORY: full list with selection ---
        with tab_history:
            st.caption("All versions are permanent. Nothing is ever overwritten or deleted.")
            for v in reversed(versions):
                cols = st.columns([1, 2, 3, 2, 1])
                cols[0].markdown(f"**v{v.version}**")
                cols[1].markdown(SOURCE_LABELS.get(v.source, v.source))
                cols[2].caption(v.note or "-")
                badges = []
                if latest_version and v.version == latest_version.version:
                    badges.append("Current")
                if v.is_final:
                    badges.append("Accepted")
                    if v.is_locked:
                        badges.append("Locked")
                cols[3].caption(" / ".join(badges) if badges else "Draft")
                if cols[4].button("View", key=f"hist_view_{v.version}"):
                    st.session_state.viewing_version = v.version
                    st.rerun()

        st.divider()

        # --- FINAL BRD: choose / unlock / download ---
        st.subheader("Final BRD")
        final_cols = st.columns([2, 2, 2])

        with final_cols[0]:
            if not viewing_version.is_final:
                if st.button(f"Choose v{viewing_version.version} as Final BRD"):
                    try:
                        service.choose_final_brd(viewing_version.version)
                        refresh_versions()
                        st.success(f"Version {viewing_version.version} is now the Accepted BRD.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))
            else:
                st.caption("This version is the Accepted BRD.")

        with final_cols[1]:
            if is_locked:
                if st.session_state.get("confirm_unlock"):
                    st.warning("Unlock the Accepted BRD? It stays in history unchanged; "
                               "any new edit creates a new version.")
                    yes_col, no_col = st.columns(2)
                    if yes_col.button("Yes, unlock"):
                        try:
                            service.unlock_final_brd()
                            st.session_state.confirm_unlock = False
                            refresh_versions()
                            st.success("Final BRD unlocked. Further edits will create a new version.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))
                    if no_col.button("Cancel"):
                        st.session_state.confirm_unlock = False
                        st.rerun()
                else:
                    if st.button("Unlock Final BRD"):
                        st.session_state.confirm_unlock = True
                        st.rerun()

        with final_cols[2]:
            # Export always uses the version being viewed, so what you see is what you download.
            if st.button("Prepare .docx for download"):
                with st.spinner("Formatting Word document..."):
                    try:
                        docx_path = (Path(settings.resolved_output_dir())
                                     / st.session_state.project_id
                                     / f"BRD_v{viewing_version.version}.docx")
                        generate_brd_docx(viewing_version.content, docx_path)
                        st.session_state.docx_ready_path = str(docx_path)
                        st.session_state.docx_ready_version = viewing_version.version
                        logger.info(f"DOCX exported for v{viewing_version.version}")
                    except Exception as exc:
                        st.error(friendly_error(exc))

            ready_path = st.session_state.get("docx_ready_path")
            ready_version = st.session_state.get("docx_ready_version")
            if ready_path and Path(ready_path).exists() and ready_version == viewing_version.version:
                try:
                    with open(ready_path, "rb") as f:
                        st.download_button(
                            f"Download BRD v{viewing_version.version}.docx",
                            data=f.read(),
                            file_name=f"BRD_v{viewing_version.version}.docx",
                            mime=("application/vnd.openxmlformats-officedocument"
                                  ".wordprocessingml.document"),
                        )
                except Exception as exc:
                    st.error(friendly_error(exc))


# --- STEP 3: HLD workspace (Solution Architect Agent) --------------------------------------

with tab_hld:
    st.caption("Phase 2: accepted BRD to High-Level Design")

    if final_version is None:
        st.warning("Accept a BRD before generating the HLD.")
        st.caption("Go to the BRD Workspace, choose a version as the Final BRD, and it will "
                   "become available here. The HLD is only ever generated from the accepted "
                   "BRD - never from the SOW or a draft.")
        st.button("Generate HLD", disabled=True)

    elif hld_latest is None:
        st.subheader("Generate the High-Level Design")
        st.caption(f"The HLD will be generated from the Accepted BRD (v{final_version.version}). "
                   "This creates HLD Version 1.")
        if st.button("Generate HLD", type="primary"):
            with st.spinner("Sending the accepted BRD to Gemini and drafting the HLD..."):
                try:
                    start = time.time()
                    hld_version = sa_service.generate_initial_hld()
                    elapsed = time.time() - start
                    logger.info(f"HLD v{hld_version.version} generated in {elapsed:.1f}s")

                    refresh_hld_versions()
                    st.session_state.hld_viewing_version = hld_version.version
                    st.success(f"HLD Version {hld_version.version} generated in {elapsed:.1f}s.")
                    st.rerun()
                except Exception as exc:
                    st.error(friendly_error(exc))

    else:
        hld_viewing_number = st.session_state.get("hld_viewing_version", hld_latest.version)
        try:
            hld_viewing = sa_service.get_version(hld_viewing_number) or hld_latest
        except Exception as exc:
            st.error(friendly_error(exc))
            hld_viewing = hld_latest

        hld_is_current = hld_viewing.version == hld_latest.version
        hld_editable = hld_is_current and not hld_is_locked

        # --- stale-vs-BRD hint (non-blocking; no auto-regeneration) ---
        try:
            if sa_service.brd_changed_since_hld():
                src = sa_service.source_brd_version()
                src_text = f"BRD v{src}" if src is not None else "an earlier BRD version"
                st.warning(
                    f"This HLD was generated from {src_text}, but the Accepted BRD is now "
                    f"v{final_version.version}. The HLD may be stale. Review it, refine it, "
                    "or start a new project to regenerate from scratch - nothing is changed "
                    "automatically."
                )
        except Exception as exc:
            st.error(friendly_error(exc))

        # --- status banner ---
        hld_status_cols = st.columns([2, 2, 2])
        with hld_status_cols[0]:
            st.metric("Viewing", f"v{hld_viewing.version}")
        with hld_status_cols[1]:
            st.metric("Type", SOURCE_LABELS.get(hld_viewing.source, hld_viewing.source))
        with hld_status_cols[2]:
            st.metric("Status", "Accepted" if hld_viewing.is_final else "Draft")

        if hld_is_locked:
            if hld_viewing.is_final:
                st.success(f"This is the Final HLD (v{hld_viewing.version}) and it is locked "
                           "against further changes.")
            else:
                st.warning(f"The Final HLD (v{hld_final.version}) is locked. "
                           "Unlock it below to make further changes.")
        elif not hld_is_current:
            st.info(f"You are viewing an older HLD version (v{hld_viewing.version}). "
                    f"Editing is only available on the current version (v{hld_latest.version}).")

        st.divider()

        hld_tab_preview, hld_tab_edit, hld_tab_refine, hld_tab_history = st.tabs(
            ["Preview", "Edit", "AI Refine", "History"]
        )

        with hld_tab_preview:
            render_artifact_markdown(hld_viewing.content)

        with hld_tab_edit:
            if not hld_editable:
                st.info("Editing is disabled for this version. "
                        + ("Unlock the Final HLD to continue." if hld_is_locked
                           else "Switch to the current version to edit."))
                st.text_area("HLD content (read-only)", value=hld_viewing.content,
                             height=500, disabled=True, key=f"hld_ro_{hld_viewing.version}")
            else:
                st.caption("Edit the HLD below. Saving creates a NEW version - "
                           "the current version is never overwritten.")
                hld_edited_text = st.text_area(
                    "HLD content (markdown)",
                    value=hld_viewing.content,
                    height=500,
                    key=f"hld_editor_{hld_viewing.version}",
                )
                hld_change_note = st.text_input(
                    "Change description (optional)",
                    placeholder="e.g. Clarified deployment topology",
                    key=f"hld_note_{hld_viewing.version}",
                )
                if st.button("Save as New Version", type="primary", key="hld_save_edit"):
                    try:
                        new_hld = sa_service.save_manual_edit(
                            hld_edited_text, note=hld_change_note.strip() or "Manual edit"
                        )
                        refresh_hld_versions()
                        st.session_state.hld_viewing_version = new_hld.version
                        st.success(f"Saved as HLD Version {new_hld.version}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

        with hld_tab_refine:
            if not hld_editable:
                st.info("AI refinement is disabled for this version. "
                        + ("Unlock the Final HLD to continue." if hld_is_locked
                           else "Switch to the current version to refine."))
            else:
                st.caption("Describe your change in plain English. The AI receives the CURRENT "
                           "HLD plus your feedback - unaffected sections are preserved.")
                hld_feedback = st.text_area(
                    "Refinement instruction",
                    placeholder="e.g. Add a caching layer for frequently accessed product data.",
                    key="hld_feedback_input",
                    height=120,
                )
                if st.button("Refine with AI", type="primary",
                             disabled=not hld_feedback.strip(), key="hld_refine_btn"):
                    with st.spinner("Sending the current HLD and your feedback to Gemini..."):
                        try:
                            start = time.time()
                            new_hld = sa_service.refine_with_ai(hld_feedback)
                            elapsed = time.time() - start
                            logger.info(f"HLD refined to v{new_hld.version} in {elapsed:.1f}s")

                            refresh_hld_versions()
                            st.session_state.hld_viewing_version = new_hld.version
                            st.success(f"Created HLD Version {new_hld.version} in {elapsed:.1f}s.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))

        with hld_tab_history:
            st.caption("All HLD versions are permanent. Nothing is ever overwritten or deleted.")
            for v in reversed(hld_versions):
                cols = st.columns([1, 2, 3, 2, 1])
                cols[0].markdown(f"**v{v.version}**")
                cols[1].markdown(SOURCE_LABELS.get(v.source, v.source))
                cols[2].caption(v.note or "-")
                badges = []
                if hld_latest and v.version == hld_latest.version:
                    badges.append("Current")
                if v.is_final:
                    badges.append("Accepted")
                    if v.is_locked:
                        badges.append("Locked")
                cols[3].caption(" / ".join(badges) if badges else "Draft")
                if cols[4].button("View", key=f"hld_hist_view_{v.version}"):
                    st.session_state.hld_viewing_version = v.version
                    st.rerun()

        st.divider()

        st.subheader("Final HLD")
        hld_final_cols = st.columns([2, 2, 2])

        with hld_final_cols[0]:
            if not hld_viewing.is_final:
                if st.button(f"Choose v{hld_viewing.version} as Final HLD", key="hld_choose_final"):
                    try:
                        sa_service.choose_final_hld(hld_viewing.version)
                        refresh_hld_versions()
                        st.success(f"HLD Version {hld_viewing.version} is now the Final HLD.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))
            else:
                st.caption("This version is the Final HLD.")

        with hld_final_cols[1]:
            if hld_is_locked:
                if st.session_state.get("hld_confirm_unlock"):
                    st.warning("Unlock the Final HLD? It stays in history unchanged; "
                               "any new edit creates a new version.")
                    yes_col, no_col = st.columns(2)
                    if yes_col.button("Yes, unlock", key="hld_unlock_yes"):
                        try:
                            sa_service.unlock_final_hld()
                            st.session_state.hld_confirm_unlock = False
                            refresh_hld_versions()
                            st.success("Final HLD unlocked. Further edits will create a new version.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))
                    if no_col.button("Cancel", key="hld_unlock_cancel"):
                        st.session_state.hld_confirm_unlock = False
                        st.rerun()
                else:
                    if st.button("Unlock Final HLD", key="hld_unlock_btn"):
                        st.session_state.hld_confirm_unlock = True
                        st.rerun()

        with hld_final_cols[2]:
            # Export always uses the version being viewed, so what you see is what you download.
            if st.button("Prepare .docx for download", key="hld_prepare_docx"):
                with st.spinner("Formatting Word document..."):
                    try:
                        hld_docx_path = (Path(settings.resolved_output_dir())
                                         / st.session_state.project_id
                                         / "hld"
                                         / f"HLD_v{hld_viewing.version}.docx")
                        generate_hld_docx(hld_viewing.content, hld_docx_path)
                        st.session_state.hld_docx_ready_path = str(hld_docx_path)
                        st.session_state.hld_docx_ready_version = hld_viewing.version
                        logger.info(f"HLD DOCX exported for v{hld_viewing.version}")
                    except Exception as exc:
                        st.error(friendly_error(exc))

            hld_ready_path = st.session_state.get("hld_docx_ready_path")
            hld_ready_version = st.session_state.get("hld_docx_ready_version")
            if (hld_ready_path and Path(hld_ready_path).exists()
                    and hld_ready_version == hld_viewing.version):
                try:
                    with open(hld_ready_path, "rb") as f:
                        st.download_button(
                            f"Download HLD v{hld_viewing.version}.docx",
                            data=f.read(),
                            file_name=f"HLD_v{hld_viewing.version}.docx",
                            mime=("application/vnd.openxmlformats-officedocument"
                                  ".wordprocessingml.document"),
                            key="hld_download_btn",
                        )
                except Exception as exc:
                    st.error(friendly_error(exc))


# --- STEP 4: User Story workspace (Initial User Story Agent) ------------------------------

with tab_stories:
    st.caption("Phase 3: accepted BRD to draft user stories")

    if final_version is None:
        st.warning("Accept a BRD before generating user stories.")
        st.caption("Go to the BRD Workspace, choose a version as the Final BRD, and it will "
                   "become available here. Draft user stories are generated only from the "
                   "accepted BRD - never from the SOW, a draft BRD, the HLD, or an LLD.")
        st.button("Generate Draft User Stories", disabled=True)

    elif us_latest is None:
        st.subheader("Generate the Draft User Stories")
        st.caption(f"The user stories will be generated from the Accepted BRD "
                   f"(v{final_version.version}). This creates User Stories Version 1.")
        if st.button("Generate Draft User Stories", type="primary"):
            with st.spinner("Sending the accepted BRD to Gemini and drafting user stories..."):
                try:
                    start = time.time()
                    us_version = us_service.generate_initial_stories()
                    elapsed = time.time() - start
                    logger.info(f"User stories v{us_version.version} generated in {elapsed:.1f}s")

                    refresh_us_versions()
                    st.session_state.us_viewing_version = us_version.version
                    st.success(f"User Stories Version {us_version.version} generated "
                               f"in {elapsed:.1f}s.")
                    st.rerun()
                except Exception as exc:
                    st.error(friendly_error(exc))

    else:
        us_viewing_number = st.session_state.get("us_viewing_version", us_latest.version)
        try:
            us_viewing = us_service.get_version(us_viewing_number) or us_latest
        except Exception as exc:
            st.error(friendly_error(exc))
            us_viewing = us_latest

        us_is_current = us_viewing.version == us_latest.version
        us_editable = us_is_current and not us_is_locked

        # --- stale-vs-BRD hint (Phase 3; non-blocking, no auto-regeneration) ---
        try:
            if us_service.brd_changed_since_stories():
                src = us_service.source_brd_version()
                src_text = f"BRD v{src}" if src is not None else "an earlier BRD version"
                st.warning(
                    f"These user stories were generated from {src_text}, but the Accepted "
                    f"BRD is now v{final_version.version}. They may be stale. Refine them "
                    "here (freeform), or reconcile them against BRD / HLD / LLD in "
                    "**Step 6: User Story Refinement** - nothing is changed automatically."
                )
        except Exception as exc:
            st.error(friendly_error(exc))

        # --- status banner ---
        us_status_cols = st.columns([2, 2, 2])
        with us_status_cols[0]:
            st.metric("Viewing", f"v{us_viewing.version}")
        with us_status_cols[1]:
            st.metric("Type", story_version_label(us_viewing))
        with us_status_cols[2]:
            st.metric("Status", "Latest" if us_is_current else "Older version")

        if us_is_locked:
            # Phase 10B: user stories are no longer a gated artifact and there is
            # no unlock control anymore. A lock can only exist on legacy data.
            st.warning(
                f"A user-story version in this project carries a legacy \"locked\" "
                f"flag (v{us_final.version}). User-story finalization was removed in "
                f"this build, so this version can no longer be edited here. The "
                f"latest version is still used by every downstream stage; create a "
                f"fresh version via **Step 6: User Story Refinement** if you need "
                f"to change the stories."
            )
        elif not us_is_current:
            st.info(f"You are viewing an older user stories version (v{us_viewing.version}). "
                    f"Editing is only available on the current version (v{us_latest.version}).")

        st.divider()

        us_tab_preview, us_tab_edit, us_tab_refine, us_tab_history = st.tabs(
            ["Preview", "Edit", "AI Refine", "History"]
        )

        with us_tab_preview:
            render_artifact_markdown(us_viewing.content)

        with us_tab_edit:
            if not us_editable:
                st.info("Editing is disabled for this version. "
                        + ("This version carries a legacy lock and can no longer be "
                           "edited (see the banner above)." if us_is_locked
                           else "Switch to the current version to edit."))
                st.text_area("User stories content (read-only)", value=us_viewing.content,
                             height=500, disabled=True, key=f"us_ro_{us_viewing.version}")
            else:
                st.caption("Edit the user stories below. Saving creates a NEW version - "
                           "the current version is never overwritten.")
                us_edited_text = st.text_area(
                    "User stories content (markdown)",
                    value=us_viewing.content,
                    height=500,
                    key=f"us_editor_{us_viewing.version}",
                )
                us_change_note = st.text_input(
                    "Change description (optional)",
                    placeholder="e.g. Reworded the checkout story",
                    key=f"us_note_{us_viewing.version}",
                )
                if st.button("Save as New Version", type="primary", key="us_save_edit"):
                    try:
                        new_us = us_service.save_manual_edit(
                            us_edited_text, note=us_change_note.strip() or "Manual edit"
                        )
                        refresh_us_versions()
                        st.session_state.us_viewing_version = new_us.version
                        st.success(f"Saved as User Stories Version {new_us.version}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

        with us_tab_refine:
            if not us_editable:
                st.info("AI refinement is disabled for this version. "
                        + ("This version carries a legacy lock and can no longer be "
                           "refined (see the banner above)." if us_is_locked
                           else "Switch to the current version to refine."))
            else:
                st.caption("Describe your change in plain English. The AI receives the CURRENT "
                           "user stories plus your feedback - unaffected stories are preserved.")
                us_feedback = st.text_area(
                    "Refinement instruction",
                    placeholder="e.g. Add a story for password reset via email.",
                    key="us_feedback_input",
                    height=120,
                )
                if st.button("Refine with AI", type="primary",
                             disabled=not us_feedback.strip(), key="us_refine_btn"):
                    with st.spinner("Sending the current user stories and your feedback to Gemini..."):
                        try:
                            start = time.time()
                            new_us = us_service.refine_with_ai(us_feedback)
                            elapsed = time.time() - start
                            logger.info(f"User stories refined to v{new_us.version} in {elapsed:.1f}s")

                            refresh_us_versions()
                            st.session_state.us_viewing_version = new_us.version
                            st.success(f"Created User Stories Version {new_us.version} "
                                       f"in {elapsed:.1f}s.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))

        with us_tab_history:
            st.caption("All user stories versions are permanent. "
                       "Nothing is ever overwritten or deleted.")
            for v in reversed(us_versions):
                cols = st.columns([1, 2, 3, 2, 1])
                cols[0].markdown(f"**v{v.version}**")
                cols[1].markdown(story_version_label(v))
                cols[2].caption(v.note or "-")
                badges = []
                if us_latest and v.version == us_latest.version:
                    badges.append("Current")
                if v.is_final:
                    badges.append("Accepted")
                    if v.is_locked:
                        badges.append("Locked")
                cols[3].caption(" / ".join(badges) if badges else "Draft")
                if cols[4].button("View", key=f"us_hist_view_{v.version}"):
                    st.session_state.us_viewing_version = v.version
                    st.rerun()

        st.divider()

        # Phase 10B: User Stories are NOT an independently gated/finalized
        # artifact in the main lifecycle. The "Choose Final" / "Unlock Final"
        # controls have been removed — downstream stages (LLD, Test Cases,
        # Refinement, Quality/Closure evidence) always consume the LATEST
        # user-story version. History is append-only; a new version is created
        # by editing / AI refine / Step 6 reconciliation.
        if any(v.is_final for v in us_versions):
            st.caption("Note: this project has a user-story version that was marked "
                       "\"final\" under an earlier build. That flag is now ignored — "
                       "the latest version is always used downstream. The historical "
                       "flag is kept in history and is not removed.")

        st.subheader("Export")
        us_export_cols = st.columns([2, 4])
        with us_export_cols[0]:
            # Export always uses the version being viewed, so what you see is what you download.
            if st.button("Prepare .docx for download", key="us_prepare_docx"):
                with st.spinner("Formatting Word document..."):
                    try:
                        us_docx_path = (Path(settings.resolved_output_dir())
                                        / st.session_state.project_id
                                        / "user_stories"
                                        / f"UserStories_v{us_viewing.version}.docx")
                        generate_user_stories_docx(us_viewing.content, us_docx_path)
                        st.session_state.us_docx_ready_path = str(us_docx_path)
                        st.session_state.us_docx_ready_version = us_viewing.version
                        logger.info(f"User stories DOCX exported for v{us_viewing.version}")
                    except Exception as exc:
                        st.error(friendly_error(exc))

            us_ready_path = st.session_state.get("us_docx_ready_path")
            us_ready_version = st.session_state.get("us_docx_ready_version")
            if (us_ready_path and Path(us_ready_path).exists()
                    and us_ready_version == us_viewing.version):
                try:
                    with open(us_ready_path, "rb") as f:
                        st.download_button(
                            f"Download UserStories v{us_viewing.version}.docx",
                            data=f.read(),
                            file_name=f"UserStories_v{us_viewing.version}.docx",
                            mime=("application/vnd.openxmlformats-officedocument"
                                  ".wordprocessingml.document"),
                            key="us_download_btn",
                        )
                except Exception as exc:
                    st.error(friendly_error(exc))


# --- STEP 5: LLD workspace (Low-Level Design Agent) ------------------------------------

with tab_lld:
    st.caption("Phase 4: accepted HLD (+ BRD / optional draft user stories) to Low-Level Design")

    if hld_final is None:
        st.warning("Accept an HLD before generating the LLD.")
        st.caption("Go to the HLD Workspace, choose a version as the Final HLD, and it will "
                   "become available here. The LLD is generated from the accepted HLD; the BRD "
                   "is supporting context and draft user stories are optional context - user "
                   "stories are never required.")
        st.button("Generate LLD", disabled=True)

    elif lld_latest is None:
        st.subheader("Generate the Low-Level Design")
        us_note = (f" Draft user stories (v{us_latest.version}) will be included as optional "
                   "context." if us_latest is not None
                   else " No draft user stories exist yet - the LLD will be generated from the "
                        "HLD and BRD only.")
        st.caption(f"The LLD will be generated from the Accepted HLD (v{hld_final.version})."
                   + us_note + " This creates LLD Version 1.")
        if st.button("Generate LLD", type="primary"):
            with st.spinner("Sending the accepted HLD and context to Gemini and drafting the LLD..."):
                try:
                    start = time.time()
                    lld_version = lld_service.generate_initial_lld()
                    elapsed = time.time() - start
                    logger.info(f"LLD v{lld_version.version} generated in {elapsed:.1f}s")

                    refresh_lld_versions()
                    st.session_state.lld_viewing_version = lld_version.version
                    st.success(f"LLD Version {lld_version.version} generated in {elapsed:.1f}s.")
                    st.rerun()
                except Exception as exc:
                    st.error(friendly_error(exc))

    else:
        lld_viewing_number = st.session_state.get("lld_viewing_version", lld_latest.version)
        try:
            lld_viewing = lld_service.get_version(lld_viewing_number) or lld_latest
        except Exception as exc:
            st.error(friendly_error(exc))
            lld_viewing = lld_latest

        lld_is_current = lld_viewing.version == lld_latest.version
        lld_editable = lld_is_current and not lld_is_locked

        # --- stale-vs-HLD hint (non-blocking; no auto-regeneration) ---
        try:
            if lld_service.hld_changed_since_lld():
                src = lld_service.source_hld_version()
                src_text = f"HLD v{src}" if src is not None else "an earlier HLD version"
                st.warning(
                    f"This LLD was generated from {src_text}, but the Accepted HLD is now "
                    f"v{hld_final.version}. The LLD may be stale. Review it, refine it, or "
                    "start a new project to regenerate from scratch - nothing is changed "
                    "automatically."
                )
        except Exception as exc:
            st.error(friendly_error(exc))

        # --- status banner ---
        lld_status_cols = st.columns([2, 2, 2])
        with lld_status_cols[0]:
            st.metric("Viewing", f"v{lld_viewing.version}")
        with lld_status_cols[1]:
            st.metric("Type", SOURCE_LABELS.get(lld_viewing.source, lld_viewing.source))
        with lld_status_cols[2]:
            st.metric("Status", "Accepted" if lld_viewing.is_final else "Draft")

        if lld_is_locked:
            if lld_viewing.is_final:
                st.success(f"This is the Final LLD (v{lld_viewing.version}) and it is locked "
                           "against further changes.")
            else:
                st.warning(f"The Final LLD (v{lld_final.version}) is locked. "
                           "Unlock it below to make further changes.")
        elif not lld_is_current:
            st.info(f"You are viewing an older LLD version (v{lld_viewing.version}). "
                    f"Editing is only available on the current version (v{lld_latest.version}).")

        st.divider()

        lld_tab_preview, lld_tab_edit, lld_tab_refine, lld_tab_history = st.tabs(
            ["Preview", "Edit", "AI Refine", "History"]
        )

        with lld_tab_preview:
            render_artifact_markdown(lld_viewing.content)

        with lld_tab_edit:
            if not lld_editable:
                st.info("Editing is disabled for this version. "
                        + ("Unlock the Final LLD to continue." if lld_is_locked
                           else "Switch to the current version to edit."))
                st.text_area("LLD content (read-only)", value=lld_viewing.content,
                             height=500, disabled=True, key=f"lld_ro_{lld_viewing.version}")
            else:
                st.caption("Edit the LLD below. Saving creates a NEW version - "
                           "the current version is never overwritten.")
                lld_edited_text = st.text_area(
                    "LLD content (markdown)",
                    value=lld_viewing.content,
                    height=500,
                    key=f"lld_editor_{lld_viewing.version}",
                )
                lld_change_note = st.text_input(
                    "Change description (optional)",
                    placeholder="e.g. Added idempotency key to the registration endpoint",
                    key=f"lld_note_{lld_viewing.version}",
                )
                if st.button("Save as New Version", type="primary", key="lld_save_edit"):
                    try:
                        new_lld = lld_service.save_manual_edit(
                            lld_edited_text, note=lld_change_note.strip() or "Manual edit"
                        )
                        refresh_lld_versions()
                        st.session_state.lld_viewing_version = new_lld.version
                        st.success(f"Saved as LLD Version {new_lld.version}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

        with lld_tab_refine:
            if not lld_editable:
                st.info("AI refinement is disabled for this version. "
                        + ("Unlock the Final LLD to continue." if lld_is_locked
                           else "Switch to the current version to refine."))
            else:
                st.caption("Describe your change in plain English. The AI receives the CURRENT "
                           "LLD plus your feedback - unaffected sections are preserved.")
                lld_feedback = st.text_area(
                    "Refinement instruction",
                    placeholder="e.g. Add a caching table for product lookups.",
                    key="lld_feedback_input",
                    height=120,
                )
                if st.button("Refine with AI", type="primary",
                             disabled=not lld_feedback.strip(), key="lld_refine_btn"):
                    with st.spinner("Sending the current LLD and your feedback to Gemini..."):
                        try:
                            start = time.time()
                            new_lld = lld_service.refine_with_ai(lld_feedback)
                            elapsed = time.time() - start
                            logger.info(f"LLD refined to v{new_lld.version} in {elapsed:.1f}s")

                            refresh_lld_versions()
                            st.session_state.lld_viewing_version = new_lld.version
                            st.success(f"Created LLD Version {new_lld.version} in {elapsed:.1f}s.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))

        with lld_tab_history:
            st.caption("All LLD versions are permanent. Nothing is ever overwritten or deleted.")
            for v in reversed(lld_versions):
                cols = st.columns([1, 2, 3, 2, 1])
                cols[0].markdown(f"**v{v.version}**")
                cols[1].markdown(SOURCE_LABELS.get(v.source, v.source))
                cols[2].caption(v.note or "-")
                badges = []
                if lld_latest and v.version == lld_latest.version:
                    badges.append("Current")
                if v.is_final:
                    badges.append("Accepted")
                    if v.is_locked:
                        badges.append("Locked")
                cols[3].caption(" / ".join(badges) if badges else "Draft")
                if cols[4].button("View", key=f"lld_hist_view_{v.version}"):
                    st.session_state.lld_viewing_version = v.version
                    st.rerun()

        st.divider()

        st.subheader("Final LLD")
        lld_final_cols = st.columns([2, 2, 2])

        with lld_final_cols[0]:
            if not lld_viewing.is_final:
                if st.button(f"Choose v{lld_viewing.version} as Final LLD", key="lld_choose_final"):
                    try:
                        lld_service.choose_final_lld(lld_viewing.version)
                        refresh_lld_versions()
                        st.success(f"LLD Version {lld_viewing.version} is now the Final LLD.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))
            else:
                st.caption("This version is the Final LLD.")

        with lld_final_cols[1]:
            if lld_is_locked:
                if st.session_state.get("lld_confirm_unlock"):
                    st.warning("Unlock the Final LLD? It stays in history unchanged; "
                               "any new edit creates a new version.")
                    yes_col, no_col = st.columns(2)
                    if yes_col.button("Yes, unlock", key="lld_unlock_yes"):
                        try:
                            lld_service.unlock_final_lld()
                            st.session_state.lld_confirm_unlock = False
                            refresh_lld_versions()
                            st.success("Final LLD unlocked. Further edits will create a new version.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))
                    if no_col.button("Cancel", key="lld_unlock_cancel"):
                        st.session_state.lld_confirm_unlock = False
                        st.rerun()
                else:
                    if st.button("Unlock Final LLD", key="lld_unlock_btn"):
                        st.session_state.lld_confirm_unlock = True
                        st.rerun()

        with lld_final_cols[2]:
            # Export always uses the version being viewed, so what you see is what you download.
            if st.button("Prepare .docx for download", key="lld_prepare_docx"):
                with st.spinner("Formatting Word document..."):
                    try:
                        lld_docx_path = (Path(settings.resolved_output_dir())
                                         / st.session_state.project_id
                                         / "lld"
                                         / f"LLD_v{lld_viewing.version}.docx")
                        generate_lld_docx(lld_viewing.content, lld_docx_path)
                        st.session_state.lld_docx_ready_path = str(lld_docx_path)
                        st.session_state.lld_docx_ready_version = lld_viewing.version
                        logger.info(f"LLD DOCX exported for v{lld_viewing.version}")
                    except Exception as exc:
                        st.error(friendly_error(exc))

            lld_ready_path = st.session_state.get("lld_docx_ready_path")
            lld_ready_version = st.session_state.get("lld_docx_ready_version")
            if (lld_ready_path and Path(lld_ready_path).exists()
                    and lld_ready_version == lld_viewing.version):
                try:
                    with open(lld_ready_path, "rb") as f:
                        st.download_button(
                            f"Download LLD v{lld_viewing.version}.docx",
                            data=f.read(),
                            file_name=f"LLD_v{lld_viewing.version}.docx",
                            mime=("application/vnd.openxmlformats-officedocument"
                                  ".wordprocessingml.document"),
                            key="lld_download_btn",
                        )
                except Exception as exc:
                    st.error(friendly_error(exc))


# --- STEP 6: User Story Refinement workspace (User Story Refinement Agent) ----------------
#
# Standalone stage. Reconciles the LATEST user-story version against the accepted BRD
# (primary) plus the accepted HLD / LLD (optional context). It writes a NEW version into
# the SAME `user_stories` stream via UserStoryRefinementService.refine() - no second store.

with tab_usr:
    st.caption("Phase 5: reconcile the current user stories against BRD (primary) + "
               "HLD / LLD (optional context). Produces a new version in the same "
               "user-story stream.")

    if final_version is None:
        st.warning("User Story Refinement is unavailable: no accepted BRD.")
        st.caption("Accept a BRD in the BRD Workspace first. The accepted BRD is the "
                   "primary business source for refinement.")

    elif us_latest is None:
        st.warning("User Story Refinement is unavailable: no user stories exist yet.")
        st.caption("Generate the initial user stories in Step 4 first. Refinement always "
                   "starts from the latest existing user-story version.")

    else:
        usr_viewing_number = st.session_state.get("usr_viewing_version", us_latest.version)
        try:
            usr_viewing = us_service.get_version(usr_viewing_number) or us_latest
        except Exception as exc:
            st.error(friendly_error(exc))
            usr_viewing = us_latest

        # --- 1. Source Artifacts -------------------------------------------------
        st.subheader("Source Artifacts")
        src_cols = st.columns(4)
        src_cols[0].metric("BRD", f"v{final_version.version}", "Accepted (required)")
        src_cols[1].metric(
            "HLD",
            f"v{hld_final.version}" if hld_final is not None else "—",
            "Accepted (context)" if hld_final is not None else "none — optional",
        )
        src_cols[2].metric(
            "LLD",
            f"v{lld_final.version}" if lld_final is not None else "—",
            "Accepted (context)" if lld_final is not None else "none — optional",
        )
        src_cols[3].metric(
            "User Stories", f"v{us_latest.version}",
            "Latest (legacy lock)" if us_is_locked else "Latest",
        )
        st.caption("HLD and LLD are optional context — refinement proceeds without them "
                   "(the agent receives a sentinel). Only accepted/final versions are used.")

        # --- 4/5. Refinement result + provenance + staleness -------------------
        if usr_is_refined and usr_recorded is not None:
            prov = (f"BRD v{usr_recorded['brd']}, "
                    f"HLD v{usr_recorded['hld'] if usr_recorded['hld'] is not None else '—'}, "
                    f"LLD v{usr_recorded['lld'] if usr_recorded['lld'] is not None else '—'}, "
                    f"source stories v{usr_recorded['us']}")
            st.success(f"Latest version **v{us_latest.version} — Artifact Refinement**. "
                       f"Built from: {prov}.")
            if us_latest.note:
                st.caption(f"Note: {us_latest.note}")

            if usr_stale:
                st.warning(
                    f"These refined stories may be **stale**: the following changed since "
                    f"the refinement — **{', '.join(usr_stale_sources)}**. Nothing is "
                    "regenerated automatically; click **Refine Again** to reconcile against "
                    "the current artifacts (creates a new version; this one stays in History)."
                )
        else:
            st.info("The latest user-story version has not been refined from artifacts yet.")

        # --- 3. Artifact Refinement action -----------------------------------
        st.divider()
        st.subheader("Artifact Refinement")
        if us_is_locked:
            st.warning("A user-story version in this project carries a legacy "
                       "\"locked\" flag. User-story finalization was removed in this "
                       "build, so refinement of a locked stream is blocked and "
                       "there is no unlock control. This only affects projects "
                       "created under an earlier build.")
        st.caption(f"Refinement starts from the current latest version "
                   f"(v{us_latest.version}), whatever its origin (initial, manual edit, or a "
                   "previous refinement). Existing US-NNN IDs and unaffected stories are "
                   "preserved; only evidence-based changes are made.")
        refine_label = "Refine Again" if usr_is_refined else "Refine Stories from Artifacts"
        if st.button(refine_label, type="primary", disabled=us_is_locked,
                     key="usr_refine_btn"):
            with st.spinner("Reconciling the user stories against BRD / HLD / LLD..."):
                try:
                    start = time.time()
                    new_us = usr_service.refine()
                    elapsed = time.time() - start
                    logger.info(f"User stories refined from artifacts to v{new_us.version} "
                                f"in {elapsed:.1f}s")
                    refresh_us_versions()
                    st.session_state.usr_viewing_version = new_us.version
                    st.session_state.us_viewing_version = new_us.version
                    st.success(f"Created User Stories Version {new_us.version} "
                               f"(Artifact Refinement) in {elapsed:.1f}s.")
                    st.rerun()
                except Exception as exc:
                    st.error(friendly_error(exc))

        # --- 2. Current User Stories: Preview + History --------------------
        st.divider()
        st.subheader("User Stories")
        usr_tab_preview, usr_tab_history = st.tabs(["Preview", "History"])

        with usr_tab_preview:
            if usr_viewing.version != us_latest.version:
                st.info(f"Viewing v{usr_viewing.version}. The latest version is "
                        f"v{us_latest.version}.")
            render_artifact_markdown(usr_viewing.content)

        with usr_tab_history:
            st.caption("The whole user-story history — initial generation, manual edits, "
                       "freeform AI refinement, and artifact refinement all share this stream.")
            for v in reversed(us_versions):
                cols = st.columns([1, 2, 3, 2, 1])
                cols[0].markdown(f"**v{v.version}**")
                cols[1].markdown(story_version_label(v))
                cols[2].caption(v.note or "-")
                badges = []
                if v.version == us_latest.version:
                    badges.append("Current")
                    if usr_stale:
                        badges.append("STALE")
                if v.is_final:
                    badges.append("Accepted")
                    if v.is_locked:
                        badges.append("Locked")
                cols[3].caption(" / ".join(badges) if badges else "Draft")
                if cols[4].button("View", key=f"usr_hist_view_{v.version}"):
                    st.session_state.usr_viewing_version = v.version
                    st.rerun()


# --- STEP 7: QA / Test Case workspace (QA / Test Case Agent) -----------------------------
#
# Standalone stage. Generates test cases from the accepted BRD (required) plus the
# accepted HLD / LLD / User Stories (optional context). Every generate / regenerate /
# manual edit / AI refine appends a NEW version to the OWN test_cases stream
# (outputs/<pid>/test_cases/versions.json) via TestCaseService - no other stream is
# written and no second copy of any artifact is created.

with tab_qa:
    st.caption("Phase 6: generate QA test cases from the accepted BRD (required) plus "
               "the accepted HLD / LLD / User Stories (optional context). Own version "
               "stream; nothing is regenerated automatically.")

    if final_version is None:
        st.warning("QA / Test Case generation is unavailable: no accepted BRD.")
        st.caption("Accept a BRD in the BRD Workspace first. The accepted BRD is the "
                   "required source of truth for test cases.")

    else:
        qa_viewing_number = st.session_state.get(
            "qa_viewing_version", qa_latest.version if qa_latest else 0
        )
        qa_viewing = None
        if qa_latest is not None:
            try:
                qa_viewing = qa_service.get_version(qa_viewing_number) or qa_latest
            except Exception as exc:
                st.error(friendly_error(exc))
                qa_viewing = qa_latest

        qa_is_current = qa_viewing is not None and qa_viewing.version == qa_latest.version
        qa_editable = qa_is_current and not qa_is_locked

        # --- 1. Source Artifacts ------------------------------------------------
        st.subheader("Source Artifacts")
        qa_src_cols = st.columns(4)
        qa_src_cols[0].metric("BRD", f"v{final_version.version}", "Accepted / Required")
        qa_src_cols[1].metric(
            "HLD",
            f"v{hld_final.version}" if hld_final is not None else "—",
            "Accepted / Context" if hld_final is not None else "none / optional",
        )
        qa_src_cols[2].metric(
            "LLD",
            f"v{lld_final.version}" if lld_final is not None else "—",
            "Accepted / Context" if lld_final is not None else "none / optional",
        )
        if us_latest is not None:
            us_label, us_state = f"v{us_latest.version}", "Latest / Context"
        else:
            us_label, us_state = "—", "none / optional"
        qa_src_cols[3].metric("User Stories", us_label, us_state)
        st.caption("HLD, LLD and User Stories are optional context - their absence never "
                   "blocks BRD-based generation. HLD/LLD use the accepted/final version; "
                   "User Stories always use the latest version.")

        if qa_latest is None:
            # --- 3. First generation ----------------------------------------
            st.divider()
            st.subheader("Generate Test Cases")
            st.caption(f"Test cases will be generated from the Accepted BRD "
                       f"(v{final_version.version}) and whatever optional context is "
                       "available. This creates Test Cases Version 1.")
            if st.button("Generate Test Cases", type="primary", key="qa_generate_btn"):
                with st.spinner("Sending the artifacts to Gemini and drafting test cases..."):
                    try:
                        start = time.time()
                        qa_v = qa_service.generate()
                        elapsed = time.time() - start
                        logger.info(f"Test cases v{qa_v.version} generated in {elapsed:.1f}s")
                        refresh_qa_versions()
                        st.session_state.qa_viewing_version = qa_v.version
                        st.success(f"Test Cases Version {qa_v.version} generated in "
                                   f"{elapsed:.1f}s.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

        else:
            # --- 5. Stale-source warning (non-blocking; no auto-regeneration) ---
            if qa_stale and qa_recorded is not None:
                parts = []
                for label, key in (("BRD", "brd"), ("HLD", "hld"), ("LLD", "lld"),
                                   ("User Stories", "us")):
                    if label in qa_stale_sources:
                        rec = qa_recorded.get(key)
                        cur = qa_current.get(key)
                        rec_txt = f"v{rec}" if rec is not None else "unavailable"
                        cur_txt = f"v{cur}" if cur is not None else "unavailable"
                        parts.append(f"{label} {rec_txt} -> {cur_txt}")
                st.warning(
                    "These test cases may be **stale** - a source artifact changed since "
                    f"they were generated: **{'; '.join(parts)}**. Nothing is regenerated "
                    "automatically. Use **Regenerate from Artifacts** or **AI Refine** to "
                    "rebuild against the current artifacts (creates a new version; this "
                    "one stays in History)."
                )

            # --- 4. Provenance of the current version ---
            if qa_recorded is not None:
                st.info(f"Current version **v{qa_latest.version}** built from: {qa_latest.note}")

            # --- status banner ---
            qa_status_cols = st.columns([2, 2, 2])
            with qa_status_cols[0]:
                st.metric("Viewing", f"v{qa_viewing.version}")
            with qa_status_cols[1]:
                st.metric("Type", SOURCE_LABELS.get(qa_viewing.source, qa_viewing.source))
            with qa_status_cols[2]:
                st.metric("Status", "Accepted" if qa_viewing.is_final else "Draft")

            if qa_is_locked:
                if qa_viewing.is_final:
                    st.success(f"This is the Final Test Cases set (v{qa_viewing.version}) "
                               "and it is locked against further changes.")
                else:
                    st.warning(f"The Final Test Cases (v{qa_final.version}) are locked. "
                               "Unlock them below to make further changes.")
            elif not qa_is_current:
                st.info(f"You are viewing an older test-case version (v{qa_viewing.version}). "
                        f"Editing is only available on the current version "
                        f"(v{qa_latest.version}).")

            st.divider()

            qa_tab_preview, qa_tab_edit, qa_tab_refine, qa_tab_history = st.tabs(
                ["Preview", "Edit", "AI Refine", "History"]
            )

            with qa_tab_preview:
                render_artifact_markdown(qa_viewing.content)

            with qa_tab_edit:
                if not qa_editable:
                    st.info("Editing is disabled for this version. "
                            + ("Unlock the Final Test Cases to continue." if qa_is_locked
                               else "Switch to the current version to edit."))
                    st.text_area("Test cases (read-only)", value=qa_viewing.content,
                                 height=500, disabled=True, key=f"qa_ro_{qa_viewing.version}")
                else:
                    st.caption("Edit the test cases below (Markdown). Saving creates a NEW "
                               "version - the current version is never overwritten. Keep "
                               "the '## TC-NNN' headings.")
                    qa_edited = st.text_area(
                        "Test cases (markdown)",
                        value=qa_viewing.content,
                        height=500,
                        key=f"qa_editor_{qa_viewing.version}",
                    )
                    qa_note = st.text_input(
                        "Change description (optional)",
                        placeholder="e.g. Corrected TC-004 expected result",
                        key=f"qa_note_{qa_viewing.version}",
                    )
                    if st.button("Save as New Version", type="primary", key="qa_save_edit"):
                        try:
                            new_qa = qa_service.save_manual_edit(
                                qa_edited, note=qa_note.strip() or "Manual edit"
                            )
                            refresh_qa_versions()
                            st.session_state.qa_viewing_version = new_qa.version
                            st.success(f"Saved as Test Cases Version {new_qa.version}.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))

            with qa_tab_refine:
                if not qa_editable:
                    st.info("AI refinement is disabled for this version. "
                            + ("Unlock the Final Test Cases to continue." if qa_is_locked
                               else "Switch to the current version to refine."))
                else:
                    st.caption("Describe your change in plain English. The AI receives the "
                               "CURRENT test cases plus the artifacts - unaffected cases and "
                               "their TC-NNN ids are preserved.")
                    qa_feedback = st.text_area(
                        "Refinement instruction",
                        placeholder="e.g. Add boundary cases for the password length rule.",
                        key="qa_feedback_input",
                        height=120,
                    )
                    if st.button("Refine with AI", type="primary",
                                 disabled=not qa_feedback.strip(), key="qa_refine_btn"):
                        with st.spinner("Sending the current test cases and your feedback "
                                        "to Gemini..."):
                            try:
                                start = time.time()
                                new_qa = qa_service.refine_with_ai(qa_feedback)
                                elapsed = time.time() - start
                                logger.info(f"Test cases refined to v{new_qa.version} in "
                                            f"{elapsed:.1f}s")
                                refresh_qa_versions()
                                st.session_state.qa_viewing_version = new_qa.version
                                st.success(f"Created Test Cases Version {new_qa.version} in "
                                           f"{elapsed:.1f}s.")
                                st.rerun()
                            except Exception as exc:
                                st.error(friendly_error(exc))

            with qa_tab_history:
                st.caption("All test-case versions are permanent. "
                           "Nothing is ever overwritten or deleted.")
                for v in reversed(qa_versions):
                    cols = st.columns([1, 2, 3, 2, 1])
                    cols[0].markdown(f"**v{v.version}**")
                    cols[1].markdown(SOURCE_LABELS.get(v.source, v.source))
                    cols[2].caption(v.note or "-")
                    badges = []
                    if v.version == qa_latest.version:
                        badges.append("Current")
                        if qa_stale:
                            badges.append("STALE")
                    if v.is_final:
                        badges.append("Accepted")
                        if v.is_locked:
                            badges.append("Locked")
                    cols[3].caption(" / ".join(badges) if badges else "Draft")
                    if cols[4].button("View", key=f"qa_hist_view_{v.version}"):
                        st.session_state.qa_viewing_version = v.version
                        st.rerun()

            st.divider()

            st.subheader("Regenerate")
            st.caption("Rebuild the test cases from scratch against the CURRENT artifacts "
                       "(ignores the current test-case content). Creates a new version.")
            if st.button("Regenerate from Artifacts", disabled=qa_is_locked,
                         key="qa_regen_btn"):
                with st.spinner("Rebuilding test cases from the current artifacts..."):
                    try:
                        start = time.time()
                        new_qa = qa_service.regenerate()
                        elapsed = time.time() - start
                        logger.info(f"Test cases regenerated to v{new_qa.version} in "
                                    f"{elapsed:.1f}s")
                        refresh_qa_versions()
                        st.session_state.qa_viewing_version = new_qa.version
                        st.success(f"Created Test Cases Version {new_qa.version} in "
                                   f"{elapsed:.1f}s.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

            st.divider()

            st.subheader("Final Test Cases")
            qa_final_cols = st.columns([2, 2, 2])

            with qa_final_cols[0]:
                if not qa_viewing.is_final:
                    if st.button(f"Choose v{qa_viewing.version} as Final Test Cases",
                                 key="qa_choose_final"):
                        try:
                            qa_service.choose_final(qa_viewing.version)
                            refresh_qa_versions()
                            st.success(f"Test Cases Version {qa_viewing.version} is now Final.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))
                else:
                    st.caption("This version is the Final Test Cases set.")

            with qa_final_cols[1]:
                if qa_is_locked:
                    if st.session_state.get("qa_confirm_unlock"):
                        st.warning("Unlock the Final Test Cases? They stay in history "
                                   "unchanged; any new edit creates a new version.")
                        yes_col, no_col = st.columns(2)
                        if yes_col.button("Yes, unlock", key="qa_unlock_yes"):
                            try:
                                qa_service.unlock_final()
                                st.session_state.qa_confirm_unlock = False
                                refresh_qa_versions()
                                st.success("Final Test Cases unlocked. Further edits will "
                                           "create a new version.")
                                st.rerun()
                            except Exception as exc:
                                st.error(friendly_error(exc))
                        if no_col.button("Cancel", key="qa_unlock_cancel"):
                            st.session_state.qa_confirm_unlock = False
                            st.rerun()
                    else:
                        if st.button("Unlock Final Test Cases", key="qa_unlock_btn"):
                            st.session_state.qa_confirm_unlock = True
                            st.rerun()

            with qa_final_cols[2]:
                if st.button("Prepare .docx for download", key="qa_prepare_docx"):
                    with st.spinner("Formatting Word document..."):
                        try:
                            qa_docx_path = (Path(settings.resolved_output_dir())
                                            / st.session_state.project_id
                                            / "test_cases"
                                            / f"TestCases_v{qa_viewing.version}.docx")
                            generate_test_cases_docx(qa_viewing.content, qa_docx_path)
                            st.session_state.qa_docx_ready_path = str(qa_docx_path)
                            st.session_state.qa_docx_ready_version = qa_viewing.version
                            logger.info(f"Test cases DOCX exported for v{qa_viewing.version}")
                        except Exception as exc:
                            st.error(friendly_error(exc))

                qa_ready_path = st.session_state.get("qa_docx_ready_path")
                qa_ready_version = st.session_state.get("qa_docx_ready_version")
                if (qa_ready_path and Path(qa_ready_path).exists()
                        and qa_ready_version == qa_viewing.version):
                    try:
                        with open(qa_ready_path, "rb") as f:
                            st.download_button(
                                f"Download TestCases v{qa_viewing.version}.docx",
                                data=f.read(),
                                file_name=f"TestCases_v{qa_viewing.version}.docx",
                                mime=("application/vnd.openxmlformats-officedocument"
                                      ".wordprocessingml.document"),
                                key="qa_download_btn",
                            )
                    except Exception as exc:
                        st.error(friendly_error(exc))


# --- STEP 8: Traceability & Quality (READ-ONLY) ------------------------------------
#
# Phase 10B. A read-only window onto the existing deterministic reporting layer.
# Phase 11A: both reports come from one memoized call to
# `build_project_reports_for_project()` (single shared service set + single
# traceability computation), keyed on the artifact-version fingerprint — so the
# matrix + extraction are NOT recomputed on every rerun, only when a stream
# actually changes. NOTHING here generates, edits, approves, unlocks, or calls
# Gemini. Every number is taken straight from those functions — no metric is
# recomputed in Streamlit.

with tab_traceability:
    st.caption("Read-only evidence view. Deterministic, computed locally from the "
               "persisted artifacts — no AI call, nothing is generated or approved "
               "here. Numbers come straight from the Project Quality Report and "
               "the Traceability matrix.")

    if latest_version is None:
        st.info("No BRD yet. Traceability & Quality evidence appears once a BRD "
                "has been generated in Step 1. (An empty project has nothing to "
                "trace.)")
    else:
        try:
            # Phase 11A: one combined, version-fingerprint-memoized call. Both
            # reports come from `build_project_reports_for_project` (single
            # shared service set + single traceability computation) and are only
            # recomputed when an artifact stream changes — not on every rerun.
            _tq_reports = _cached_project_reports(
                st.session_state.project_id, _artifact_fp
            )
            _tq_quality = _tq_reports["quality"]
            _tq_trace = _tq_reports["traceability"]
        except Exception as exc:
            _tq_quality = _tq_trace = None
            st.error(friendly_error(exc))

        if _tq_quality is not None:
            _astat = _tq_quality["artifact_status"]
            if not _astat["test_cases"]["exists"]:
                st.info("This is a partial project — some artifacts have not been "
                        "generated yet. The evidence below reflects only what "
                        "currently exists; missing artifacts are shown as "
                        "\"not generated\".")

            # --- 1. Artifact status -------------------------------------------
            st.subheader("Artifact status")
            _astat_rows = []
            for _key, _label in (("brd", "BRD"), ("hld", "HLD"),
                                 ("user_stories", "User Stories"), ("lld", "LLD"),
                                 ("test_cases", "Test Cases")):
                _i = _astat[_key]
                _fin = _i["final_version"]
                _astat_rows.append({
                    "Artifact": _label,
                    "Exists": "Yes" if _i["exists"] else "No",
                    "Latest version": f"v{_i['latest_version']}" if _i["latest_version"] else "—",
                    "Final version": (
                        "N/A (no finalization stage)" if _key == "user_stories"
                        else (f"v{_fin}" if _fin is not None else ("None" if _i["exists"] else "—"))
                    ),
                })
            st.dataframe(_astat_rows, use_container_width=True, hide_index=True)
            st.caption("User Stories are not an independently finalized artifact in "
                       "the main lifecycle — the latest version is always used "
                       "downstream.")

            # --- 2. Requirement coverage ------------------------------------
            _rc = _tq_quality["requirement_coverage"]
            st.subheader("Requirement coverage")
            _rc_cols = st.columns(4)
            _rc_cols[0].metric("Total requirements", _rc["total"])
            _rc_cols[1].metric("Covered", _rc["covered"])
            _rc_cols[2].metric("Uncovered", _rc["total"] - _rc["covered"])
            _rc_cols[3].metric("Coverage", f"{_rc['coverage_pct']}%")
            _by_kind = _rc.get("by_kind") or {}
            if _by_kind:
                _kind_labels = {"FR": "Functional Requirements",
                                "NFR": "Non-Functional Requirements",
                                "BR": "Business Requirements"}
                _bk_rows = [{
                    "Requirement kind": _kind_labels.get(_k, _k),
                    "Total": _v["total"], "Covered": _v["covered"],
                    "Coverage %": f"{_v['coverage_pct']}%",
                } for _k, _v in _by_kind.items()]
                st.dataframe(_bk_rows, use_container_width=True, hide_index=True)
            if _rc["uncovered_ids"]:
                with st.expander(f"Uncovered requirement IDs ({len(_rc['uncovered_ids'])})"):
                    st.write(", ".join(map(str, _rc["uncovered_ids"])))
            st.caption("\"Covered\" means the requirement has at least one linked "
                       "user story AND at least one linked test case (direct or "
                       "story-mediated) — the existing traceability-matrix "
                       "definition.")

            # --- 3. User story coverage -----------------------------------
            _uc = _tq_quality["user_story_coverage"]
            st.subheader("User story coverage")
            _uc_cols = st.columns(4)
            _uc_cols[0].metric("Total user stories", _uc["total"])
            _uc_cols[1].metric("Covered by test cases", _uc["covered"])
            _uc_cols[2].metric("Uncovered", _uc["total"] - _uc["covered"])
            _uc_cols[3].metric("Coverage", f"{_uc['coverage_pct']}%")
            if _uc["uncovered_ids"]:
                with st.expander(f"Uncovered user story IDs ({len(_uc['uncovered_ids'])})"):
                    st.write(", ".join(map(str, _uc["uncovered_ids"])))

            # --- 4. Test case reference population -----------------------
            st.subheader("Test case reference population")
            st.caption("How many test cases populate each reference field. This is "
                       "traceability *evidence*, not the same thing as full "
                       "requirement coverage — a populated field only means a "
                       "reference was written, not that it is grounded or covers a "
                       "requirement end-to-end.")
            _pop = _tq_quality["test_case_reference_population"]
            _pop_rows = [{
                "Reference field": _f.replace("_", " "),
                "Populated": f"{_d['populated']} of {_d['total']}",
                "Schema-required": "Yes" if _d["required"] else "No (optional context)",
            } for _f, _d in _pop.items()]
            st.dataframe(_pop_rows, use_container_width=True, hide_index=True)

            # --- 5. Grounding findings ---------------------------------
            _gf = _tq_quality["grounding_findings"]
            st.subheader("Grounding findings")
            st.metric("Ungrounded references", _gf["total"])
            if _gf["entries"]:
                st.caption("A cited reference that was not found in the artifact it "
                           "points at. Diagnostic only.")
                st.dataframe(_gf["entries"], use_container_width=True, hide_index=True)
            else:
                st.caption("No ungrounded references. (Diagnostic — does not affect "
                           "coverage numbers.)")

            # --- 6. Orphan references --------------------------------
            _orf = _tq_quality["orphan_references"]
            st.subheader("Orphan references")
            st.metric("Orphan references", _orf["total"])
            if _orf["entries"]:
                st.caption("A test-case reference that matches no known requirement "
                           "or user story. Diagnostic only — orphans never change "
                           "the coverage calculations above.")
                st.dataframe(_orf["entries"], use_container_width=True, hide_index=True)
            else:
                st.caption("No orphan references. (Diagnostic — does not affect "
                           "coverage numbers.)")

            # --- 7. Traceability matrix -----------------------------
            st.subheader("Traceability matrix")
            _matrix = (_tq_trace or {}).get("traceability_matrix") or []
            if not _matrix:
                st.info("No requirements extracted from the BRD yet — the matrix is "
                        "empty.")
            else:
                _mrows = [{
                    "requirement_id": _r["requirement_id"],
                    "requirement_kind": _r["requirement_kind"],
                    "requirement_title": _r.get("requirement_title") or "",
                    "user_story_ids": ", ".join(_r["user_story_ids"]),
                    "test_case_ids": ", ".join(_r["test_case_ids"]),
                    "test_case_ids_direct": ", ".join(_r["test_case_ids_direct"]),
                    "test_case_ids_via_story": ", ".join(_r["test_case_ids_via_story"]),
                    "has_user_stories": "Yes" if _r["has_user_stories"] else "No",
                    "has_test_cases": "Yes" if _r["has_test_cases"] else "No",
                    "is_covered": "Yes" if _r["is_covered"] else "No",
                } for _r in _matrix]
                st.dataframe(_mrows, use_container_width=True, hide_index=True)
                st.caption("Read-only. `test_case_ids` is the de-duplicated union of "
                           "`test_case_ids_direct` and `test_case_ids_via_story`.")


# --- STEP 9: Closure Report -----------------------------------------------------------
#
# Phase 7 (renumbered to Step 9 in Phase 10B). Additive, mirrors the other
# artifact tabs. The Closure Report is the final evidence-based synthesis of the
# SDLC; it CONSUMES the Traceability and Project Quality reports (read-only) plus
# the persisted artifacts. Only a final BRD is a hard prerequisite; every other
# missing/incomplete artifact is represented inside the report. Generation NEVER
# finalizes — "Choose Final" is a separate, human-only action, exactly like every
# other stage. A non-blocking staleness banner (Phase 10B) tells the user when the
# report's evidence is out of date; it never regenerates or re-approves anything.

with tab_closure:
    st.caption("Phase 7: the final project closure assessment. Deterministic facts "
               "(artifact status, coverage, findings, closure status) are computed "
               "from the existing Traceability and Project Quality reports; Gemini "
               "writes only the narrative. Own version stream; nothing is "
               "regenerated automatically; the platform never finalizes it.")

    if final_version is None:
        st.warning("Closure Report generation is unavailable: no accepted BRD.")
        st.caption("Accept a BRD in the BRD Workspace first. A final BRD is the only "
                   "hard prerequisite — a missing HLD / LLD / User Stories / Test "
                   "Cases is reported as a finding, not a blocker.")
    else:
        closure_viewing_number = st.session_state.get(
            "closure_viewing_version", closure_latest.version if closure_latest else 0
        )
        closure_viewing = None
        if closure_latest is not None:
            try:
                closure_viewing = (
                    closure_service.get_version(closure_viewing_number) or closure_latest
                )
            except Exception as exc:
                st.error(friendly_error(exc))
                closure_viewing = closure_latest

        if closure_latest is None:
            st.divider()
            st.subheader("Generate Closure Report")
            st.caption("Builds the closure report from the current project evidence "
                       "(BRD, HLD, User Stories, LLD, Test Cases, Traceability, "
                       "Project Quality Report). Creates Closure Report Version 1.")
            if st.button("Generate Closure Report", type="primary",
                         key="closure_generate_btn"):
                with st.spinner("Assembling project evidence and drafting the closure "
                                "report..."):
                    try:
                        start = time.time()
                        cr_v = closure_service.generate()
                        elapsed = time.time() - start
                        logger.info(f"Closure report v{cr_v.version} generated in "
                                    f"{elapsed:.1f}s")
                        refresh_closure_versions()
                        st.session_state.closure_viewing_version = cr_v.version
                        st.success(f"Closure Report Version {cr_v.version} generated in "
                                   f"{elapsed:.1f}s.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))
        else:
            closure_is_current = (
                closure_viewing is not None
                and closure_viewing.version == closure_latest.version
            )

            cr_status_cols = st.columns([2, 2, 2])
            with cr_status_cols[0]:
                st.metric("Viewing", f"v{closure_viewing.version}")
            with cr_status_cols[1]:
                st.metric("Type", SOURCE_LABELS.get(closure_viewing.source,
                                                    closure_viewing.source))
            with cr_status_cols[2]:
                st.metric("Status", "Accepted" if closure_viewing.is_final else "Draft")

            st.info(f"Current version **v{closure_latest.version}** — {closure_latest.note}")

            # Phase 10B: non-blocking staleness. The report is NEVER regenerated,
            # re-approved, unlocked, or replaced automatically — this only tells
            # the human that the evidence has moved on. (Phase 11A: memoized on
            # the artifact-version fingerprint — recomputed only when a stream
            # changes, not on every rerun.)
            _cr_staleness = _cached_closure_staleness(
                st.session_state.project_id, _artifact_fp
            )
            _cr_stale_sources = _cr_staleness["stale_sources"]
            if _cr_stale_sources:
                _rec = _cr_staleness["recorded"]
                _cur = _cr_staleness["current"]
                _keymap = {"BRD": "brd", "HLD": "hld", "LLD": "lld",
                           "User Stories": "us", "Test Cases": "tc"}

                def _vtxt(v):
                    return f"v{v}" if v is not None else "—"

                _parts = [
                    f"{name} {_vtxt(_rec.get(_keymap[name]))} → {_vtxt(_cur.get(_keymap[name]))}"
                    for name in _cr_stale_sources
                ]
                st.warning(
                    "**This Closure Report's evidence may be stale.** The following "
                    "changed after it was generated: **" + "; ".join(_parts) + "**. "
                    "The report below is unchanged and still reflects the older "
                    "evidence. Nothing is regenerated, re-approved, or unlocked "
                    "automatically — use **Regenerate from Project Evidence** below "
                    "if you want a report built from the current state, then review "
                    "and finalize it yourself."
                )

            if closure_is_locked:
                if closure_viewing.is_final:
                    st.success(f"This is the Final Closure Report (v{closure_viewing.version}) "
                               "and it is locked against further changes.")
                else:
                    st.warning(f"The Final Closure Report (v{closure_final.version}) is "
                               "locked. Unlock it below to regenerate.")
            elif not closure_is_current:
                st.info(f"You are viewing an older version (v{closure_viewing.version}). "
                        f"The current version is v{closure_latest.version}.")

            st.divider()

            cr_tab_preview, cr_tab_history = st.tabs(["Preview", "History"])
            with cr_tab_preview:
                render_artifact_markdown(closure_viewing.content)
            with cr_tab_history:
                for v in reversed(closure_versions):
                    cols = st.columns([3, 2, 2])
                    cols[0].write(f"**v{v.version}** — {SOURCE_LABELS.get(v.source, v.source)}")
                    cols[1].write("Accepted" if v.is_final else "Draft")
                    if cols[2].button(f"View v{v.version}", key=f"closure_hist_view_{v.version}"):
                        st.session_state.closure_viewing_version = v.version
                        st.rerun()

            st.divider()

            st.subheader("Regenerate")
            st.caption("Rebuild the closure report from the CURRENT project evidence. "
                       "Creates a new version; the previous one stays in History.")
            if st.button("Regenerate from Project Evidence", disabled=closure_is_locked,
                         key="closure_regen_btn"):
                with st.spinner("Rebuilding the closure report from current evidence..."):
                    try:
                        start = time.time()
                        new_cr = closure_service.regenerate()
                        elapsed = time.time() - start
                        logger.info(f"Closure report regenerated to v{new_cr.version} in "
                                    f"{elapsed:.1f}s")
                        refresh_closure_versions()
                        st.session_state.closure_viewing_version = new_cr.version
                        st.success(f"Created Closure Report Version {new_cr.version} in "
                                   f"{elapsed:.1f}s.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

            st.divider()

            st.subheader("Final Closure Report")
            cr_final_cols = st.columns([2, 2, 2])

            with cr_final_cols[0]:
                if not closure_viewing.is_final:
                    if st.button(f"Choose v{closure_viewing.version} as Final Closure Report",
                                 key="closure_choose_final"):
                        try:
                            closure_service.choose_final(closure_viewing.version)
                            refresh_closure_versions()
                            st.success(f"Closure Report Version {closure_viewing.version} "
                                       "is now Final.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))
                else:
                    st.caption("This version is the Final Closure Report.")

            with cr_final_cols[1]:
                if closure_is_locked:
                    if st.session_state.get("closure_confirm_unlock"):
                        st.warning("Unlock the Final Closure Report? It stays in history "
                                   "unchanged; regenerating creates a new version.")
                        yes_col, no_col = st.columns(2)
                        if yes_col.button("Yes, unlock", key="closure_unlock_yes"):
                            try:
                                closure_service.unlock_final()
                                st.session_state.closure_confirm_unlock = False
                                refresh_closure_versions()
                                st.success("Final Closure Report unlocked.")
                                st.rerun()
                            except Exception as exc:
                                st.error(friendly_error(exc))
                        if no_col.button("Cancel", key="closure_unlock_cancel"):
                            st.session_state.closure_confirm_unlock = False
                            st.rerun()
                    else:
                        if st.button("Unlock Final Closure Report", key="closure_unlock_btn"):
                            st.session_state.closure_confirm_unlock = True
                            st.rerun()

            with cr_final_cols[2]:
                if st.button("Prepare .docx for download", key="closure_prepare_docx"):
                    with st.spinner("Formatting Word document..."):
                        try:
                            cr_docx_path = (Path(settings.resolved_output_dir())
                                            / st.session_state.project_id
                                            / "closure_report"
                                            / f"ClosureReport_v{closure_viewing.version}.docx")
                            generate_closure_report_docx(closure_viewing.content, cr_docx_path)
                            st.session_state.closure_docx_ready_path = str(cr_docx_path)
                            st.session_state.closure_docx_ready_version = closure_viewing.version
                            logger.info(f"Closure report DOCX exported for "
                                        f"v{closure_viewing.version}")
                        except Exception as exc:
                            st.error(friendly_error(exc))

                cr_ready_path = st.session_state.get("closure_docx_ready_path")
                cr_ready_version = st.session_state.get("closure_docx_ready_version")
                if (cr_ready_path and Path(cr_ready_path).exists()
                        and cr_ready_version == closure_viewing.version):
                    try:
                        with open(cr_ready_path, "rb") as f:
                            st.download_button(
                                f"Download ClosureReport v{closure_viewing.version}.docx",
                                data=f.read(),
                                file_name=f"ClosureReport_v{closure_viewing.version}.docx",
                                mime=("application/vnd.openxmlformats-officedocument"
                                      ".wordprocessingml.document"),
                                key="closure_download_btn",
                            )
                    except Exception as exc:
                        st.error(friendly_error(exc))
