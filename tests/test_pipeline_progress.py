"""
Phase 15B — pipeline progress infrastructure.

Deterministic: the state-model / collector / formatter tests build their inputs
by hand (no wall-clock, no Gemini). The few `run_step` integration cases use the
existing stub agents from `tests/conftest.py` (no network). Nothing here touches
persistence beyond the autouse `isolated_output_dir` tmp dir.

Also asserts the Phase 15B change to `app/orchestration/graph.py` is surgical:
graph topology, `SDLCState` fields, and the existing structured log output are
unchanged, and no dependency was added.
"""

from __future__ import annotations

import ast
import copy
import inspect
import logging
from pathlib import Path

import pytest

from app.observability.pipeline_progress import (
    BLOCKED,
    COMPLETED,
    FAILED,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    PENDING,
    PHASE_RUN_COMPLETED,
    PHASE_RUN_FAILED,
    PHASE_RUN_STARTED,
    PHASE_STAGE_COMPLETED,
    PHASE_STAGE_FAILED,
    PHASE_STAGE_STARTED,
    READONLY,
    RUNNING,
    STAGE_LABELS,
    WAITING_FOR_APPROVAL,
    PipelineEvent,
    PipelineProgress,
    format_elapsed,
    format_elapsed_ms,
    pipeline_stage_model,
    stage_label,
)
from app.orchestration.graph import build_sdlc_graph, run_step

_TELEMETRY_LOGGER = "app.telemetry"
_GRAPH_LOGGER = "app.orchestration.graph"


# --- an sdlc_status()-shaped dict (same field set as the real snapshot) ------

def _status(**overrides) -> dict:
    base = {
        "project_id": "p",
        "brd_exists": False, "brd_latest_version": None, "brd_final_version": None,
        "awaiting_brd_approval": False,
        "hld_exists": False, "hld_latest_version": None, "hld_final_version": None,
        "awaiting_hld_approval": False,
        "us_exists": False, "us_latest_version": None,
        "lld_exists": False, "lld_latest_version": None, "lld_final_version": None,
        "awaiting_lld_approval": False,
        "tc_exists": False, "tc_latest_version": None, "tc_final_version": None,
        "awaiting_test_cases_approval": False,
        "closure_exists": False, "closure_latest_version": None,
        "closure_final_version": None, "awaiting_closure_approval": False,
        "closure_report_stale": False, "closure_report_stale_sources": [],
        "next_step": "generate_brd",
    }
    base.update(overrides)
    return base


def _by_step(rows):
    return {r["step"]: r for r in rows}


# ==========================================================================
# 1. Fresh project -> PENDING / READONLY
# ==========================================================================

def test_fresh_project_is_all_pending():
    rows = pipeline_stage_model(_status(next_step="generate_brd"))
    assert [r["step"] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert [r["label"] for r in rows] == [
        "SOW → BRD", "BRD Workspace", "HLD Workspace", "User Story Workspace",
        "LLD Workspace", "User Story Refinement", "QA / Test Case Workspace",
        "Traceability & Quality", "Closure Report",
    ]
    by = _by_step(rows)
    assert all(by[n]["state"] == PENDING for n in range(1, 10))
    assert by[1]["is_next"] is True
    assert sum(1 for r in rows if r["is_next"]) == 1


def test_readonly_steps_are_readonly_once_prerequisites_exist():
    rows = _by_step(pipeline_stage_model(_status(
        brd_exists=True, brd_final_version=1, us_exists=True, next_step="generate_hld",
    )))
    assert rows[6]["state"] == READONLY          # User Story Refinement (us exists)
    assert rows[8]["state"] == READONLY          # Traceability & Quality (brd exists)
    assert rows[1]["state"] == COMPLETED
    assert rows[2]["state"] == COMPLETED
    assert rows[3]["state"] == PENDING and rows[3]["is_next"] is True


# ==========================================================================
# 2. Approval gates -> WAITING_FOR_APPROVAL
# ==========================================================================

@pytest.mark.parametrize("next_step, step_num, extra", [
    ("approve_brd", 2, dict(brd_exists=True, awaiting_brd_approval=True)),
    ("approve_hld", 3, dict(brd_exists=True, brd_final_version=1,
                            hld_exists=True, awaiting_hld_approval=True)),
    ("approve_lld", 5, dict(brd_exists=True, brd_final_version=1,
                            hld_exists=True, hld_final_version=1,
                            lld_exists=True, awaiting_lld_approval=True)),
    ("approve_test_cases", 7, dict(brd_exists=True, brd_final_version=1,
                                   hld_exists=True, hld_final_version=1,
                                   lld_exists=True, lld_final_version=1,
                                   tc_exists=True, awaiting_test_cases_approval=True)),
    ("approve_closure_report", 9, dict(brd_exists=True, brd_final_version=1,
                                       tc_exists=True, tc_final_version=1,
                                       closure_exists=True,
                                       awaiting_closure_approval=True)),
    ("review_closure_report", 9, dict(brd_exists=True, brd_final_version=1,
                                      closure_exists=True, closure_final_version=1,
                                      closure_report_stale=True)),
])
def test_approval_gate_is_waiting_for_approval(next_step, step_num, extra):
    rows = _by_step(pipeline_stage_model(_status(next_step=next_step, **extra)))
    assert rows[step_num]["state"] == WAITING_FOR_APPROVAL
    assert rows[step_num]["is_next"] is True


# ==========================================================================
# 3. Generation next steps -> PENDING
# ==========================================================================

@pytest.mark.parametrize("next_step, step_num", [
    ("generate_brd", 1),
    ("generate_hld", 3),
    ("generate_user_stories", 4),   # tolerated (sdlc_status never emits it today)
    ("generate_lld", 5),
    ("generate_test_cases", 7),
    ("generate_closure_report", 9),
])
def test_generation_next_step_is_pending(next_step, step_num):
    rows = _by_step(pipeline_stage_model(_status(next_step=next_step)))
    assert rows[step_num]["state"] == PENDING
    assert rows[step_num]["is_next"] is True


# ==========================================================================
# 4. Completed stages
# ==========================================================================

def test_completed_pipeline_marks_every_step_done_or_readonly():
    rows = _by_step(pipeline_stage_model(_status(
        brd_exists=True, brd_final_version=1,
        hld_exists=True, hld_final_version=1,
        us_exists=True,
        lld_exists=True, lld_final_version=1,
        tc_exists=True, tc_final_version=1,
        closure_exists=True, closure_final_version=1,
        next_step=None,
    )))
    assert [rows[n]["state"] for n in range(1, 10)] == [
        COMPLETED, COMPLETED, COMPLETED, COMPLETED, COMPLETED,
        READONLY, COMPLETED, READONLY, COMPLETED,
    ]
    assert all(r["is_next"] is False for r in rows.values())


# ==========================================================================
# 5. Run overlay: COMPLETED / RUNNING / elapsed / FAILED / BLOCKED
# ==========================================================================

_OVERLAY_BASE = dict(
    brd_exists=True, brd_final_version=1,
    hld_exists=True, hld_final_version=1,
    us_exists=True, next_step="generate_lld",
)


def test_run_overlay_completed_running_and_elapsed():
    recs = {
        "run_id": "r1",
        "stages": {
            "brd": {"state": COMPLETED, "elapsed_ms": 1200, "attempts": 1},
            "hld": {"state": COMPLETED, "elapsed_ms": 8000, "attempts": 1},
            "user_stories": {"state": COMPLETED, "elapsed_ms": 3000, "attempts": None},
            "lld": {"state": RUNNING, "elapsed_ms": None, "attempts": 1},
        },
        "failure_stage": None,
    }
    rows = _by_step(pipeline_stage_model(_status(**_OVERLAY_BASE), recs))
    assert rows[1]["state"] == COMPLETED and rows[1]["elapsed_ms"] == 1200
    assert rows[3]["state"] == COMPLETED and rows[3]["elapsed_ms"] == 8000
    assert rows[4]["state"] == COMPLETED and rows[4]["elapsed_ms"] == 3000
    assert rows[5]["state"] == RUNNING and rows[5]["elapsed_ms"] is None
    assert rows[7]["state"] == PENDING      # no record, no failure -> base state
    assert rows[9]["state"] == PENDING
    assert rows[6]["state"] == READONLY
    assert rows[8]["state"] == READONLY


def test_run_overlay_failed_stage_blocks_downstream():
    recs = {
        "run_id": "r1",
        "stages": {
            "brd": {"state": COMPLETED, "elapsed_ms": 1000},
            "hld": {"state": COMPLETED, "elapsed_ms": 9000},
            "user_stories": {"state": COMPLETED, "elapsed_ms": 3000},
            "lld": {"state": FAILED, "elapsed_ms": 500, "attempts": 3},
        },
        "failure_stage": "lld",
    }
    rows = _by_step(pipeline_stage_model(_status(**_OVERLAY_BASE), recs))
    assert rows[5]["state"] == FAILED
    assert rows[5]["is_failure"] is True
    assert rows[5]["attempts"] == 3
    assert rows[7]["state"] == BLOCKED
    assert rows[9]["state"] == BLOCKED
    assert rows[6]["state"] == READONLY          # readonly steps are never blocked
    assert rows[8]["state"] == READONLY
    assert rows[1]["state"] == rows[3]["state"] == rows[4]["state"] == COMPLETED
    assert all(r["is_next"] is False for r in rows.values())   # failure suppresses is_next


def test_run_overlay_keeps_waiting_for_approval_after_generation():
    # HLD was generated THIS run; the persisted status now says it awaits approval.
    base = _status(
        brd_exists=True, brd_final_version=1,
        hld_exists=True, hld_final_version=None, awaiting_hld_approval=True,
        us_exists=True, next_step="approve_hld",
    )
    recs = {"stages": {"hld": {"state": COMPLETED, "elapsed_ms": 8000}}, "failure_stage": None}
    rows = _by_step(pipeline_stage_model(base, recs))
    assert rows[3]["state"] == WAITING_FOR_APPROVAL
    assert rows[3]["elapsed_ms"] == 8000


# ==========================================================================
# 6. Structured collector: order + folding
# ==========================================================================

def test_pipeline_progress_records_events_in_order_and_folds_them():
    incoming = [
        PipelineEvent(phase=PHASE_RUN_STARTED, run_id="rid-1"),
        PipelineEvent(phase=PHASE_STAGE_STARTED, stage="brd", run_id="rid-1"),
        PipelineEvent(phase=PHASE_STAGE_COMPLETED, stage="brd", run_id="rid-1",
                      elapsed_ms=1200, outcome=OUTCOME_SUCCESS),
        PipelineEvent(phase=PHASE_STAGE_STARTED, stage="hld", run_id="rid-1"),
        PipelineEvent(phase=PHASE_STAGE_FAILED, stage="hld", run_id="rid-1",
                      elapsed_ms=500, outcome=OUTCOME_ERROR),
        PipelineEvent(phase=PHASE_RUN_FAILED, run_id="rid-1", failure_stage="hld",
                      outcome=OUTCOME_ERROR, elapsed_ms=1900),
    ]
    p = PipelineProgress()
    for ev in incoming:
        p.record(ev)

    got = p.events
    assert [e.phase for e in got] == [e.phase for e in incoming]
    assert [e.seq for e in got] == list(range(len(incoming)))
    assert p.run_id == "rid-1"

    recs = p.as_run_records()
    assert recs["run_id"] == "rid-1"
    assert recs["failure_stage"] == "hld"
    assert recs["stages"]["brd"] == {"state": COMPLETED, "elapsed_ms": 1200, "attempts": None}
    assert recs["stages"]["hld"]["state"] == FAILED
    assert recs["stages"]["hld"]["elapsed_ms"] == 500


def test_pipeline_progress_is_callable_accepts_mappings_and_never_raises():
    p = PipelineProgress()
    p({"phase": PHASE_STAGE_STARTED, "stage": "brd"})   # __call__ + mapping input
    assert p.events[0].stage == "brd"
    p.record("not a mapping")                           # dropped, no exception
    p.record({"bogus_field": 1})                        # dropped, no exception
    assert len(p.events) == 1


def test_as_run_records_picks_up_attempts_when_present():
    p = PipelineProgress()
    p.record(PipelineEvent(phase=PHASE_STAGE_COMPLETED, stage="test_cases",
                           elapsed_ms=9000, attempt=2))
    p.record(PipelineEvent(phase=PHASE_RUN_COMPLETED, pipeline_status="complete",
                           awaiting=None, produced={"tc": 3}))
    recs = p.as_run_records()
    assert recs["stages"]["test_cases"]["attempts"] == 2
    assert recs["pipeline_status"] == "complete"
    assert recs["produced"] == {"tc": 3}


# ==========================================================================
# 7. run_step behaviour unchanged when the callback is omitted
# ==========================================================================

def _services(pid):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.closure_report.service import ClosureReportService
    from app.agents.initial_user_story.service import InitialUserStoryService
    from app.agents.low_level_design.service import LowLevelDesignService
    from app.agents.solution_architect.service import SolutionArchitectService
    from app.agents.test_case.service import TestCaseService
    from tests.conftest import (
        StubBAAgent, StubClosureReportAgent, StubLLDAgent, StubSAAgent,
        StubTestCaseAgent, StubUserStoryAgent,
    )

    ba = BusinessAnalystService(project_id=pid, agent=StubBAAgent())
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=StubSAAgent())
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=StubUserStoryAgent())
    lld = LowLevelDesignService(project_id=pid, sa_service=sa, ba_service=ba,
                                agent=StubLLDAgent())
    tc = TestCaseService(project_id=pid, agent=StubTestCaseAgent())
    cr = ClosureReportService(project_id=pid, agent=StubClosureReportAgent())
    return dict(ba_service=ba, sa_service=sa, us_service=us, lld_service=lld,
                tc_service=tc, closure_service=cr)


def test_run_step_result_is_identical_with_and_without_callback(sow_file, sample_metadata):
    a = run_step("p15b_noev", "ensure_brd", sow_path=str(sow_file),
                 metadata=sample_metadata, **_services("p15b_noev"))
    b = run_step("p15b_ev", "ensure_brd", sow_path=str(sow_file),
                 metadata=sample_metadata, on_event=PipelineProgress(),
                 **_services("p15b_ev"))
    c = run_step("p15b_noop", "ensure_brd", sow_path=str(sow_file),
                 metadata=sample_metadata, on_event=lambda e: None,
                 **_services("p15b_noop"))
    for state in (a, b, c):
        assert state["status"] == "awaiting_approval"
        assert state["awaiting"] == "brd_final"
        assert state["produced"] == {"brd": 1}
    # the derived pointer keys match too (project_id/request/sow_path/metadata differ by design)
    keys = ("status", "awaiting", "produced", "brd_latest_version", "brd_final_version")
    assert {k: a.get(k) for k in keys} == {k: b.get(k) for k in keys} == {k: c.get(k) for k in keys}


def test_callback_does_not_change_run_step_structured_log_output(
    sow_file, sample_metadata, monkeypatch, caplog,
):
    monkeypatch.setattr(logging.getLogger(_GRAPH_LOGGER), "propagate", True)
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        run_step("p15b_log", "ensure_brd", sow_path=str(sow_file),
                 metadata=sample_metadata, on_event=PipelineProgress(),
                 **_services("p15b_log"))
    msgs = [r.getMessage() for r in caplog.records if r.name == _GRAPH_LOGGER]
    assert any(m.startswith("run_step start run_id=") for m in msgs)
    assert any(m.startswith("run_step complete run_id=") for m in msgs)


# ==========================================================================
# 8. Failure: correct stage, no exception text
# ==========================================================================

_LEAKY_DETAIL = "boom-secret-provider-detail-503-upstream"


class _BoomSAAgent:
    """A stub Solution Architect agent that always raises (with a secret-looking
    message) so we can prove the callback never surfaces provider text."""

    def generate_hld(self, brd_text, metadata):
        from app.agents.solution_architect.agent import SolutionArchitectAgentError
        raise SolutionArchitectAgentError(f"Gemini API call failed: {_LEAKY_DETAIL}")

    def refine_hld(self, *a, **k):  # pragma: no cover - not exercised
        from app.agents.solution_architect.agent import SolutionArchitectAgentError
        raise SolutionArchitectAgentError(_LEAKY_DETAIL)


def test_failure_event_names_the_stage_without_exposing_exception_text(
    sow_file, sample_metadata,
):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.closure_report.service import ClosureReportService
    from app.agents.initial_user_story.service import InitialUserStoryService
    from app.agents.low_level_design.service import LowLevelDesignService
    from app.agents.solution_architect.agent import SolutionArchitectAgentError
    from app.agents.solution_architect.service import SolutionArchitectService
    from app.agents.test_case.service import TestCaseService
    from tests.conftest import (
        StubBAAgent, StubClosureReportAgent, StubLLDAgent, StubTestCaseAgent,
        StubUserStoryAgent,
    )

    pid = "p15b_fail"
    ba = BusinessAnalystService(project_id=pid, agent=StubBAAgent())
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)                       # so run_step advances past gate_brd

    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=_BoomSAAgent())
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=StubUserStoryAgent())
    lld = LowLevelDesignService(project_id=pid, sa_service=sa, ba_service=ba,
                                agent=StubLLDAgent())
    tc = TestCaseService(project_id=pid, agent=StubTestCaseAgent())
    cr = ClosureReportService(project_id=pid, agent=StubClosureReportAgent())

    progress = PipelineProgress()
    with pytest.raises(SolutionArchitectAgentError):
        run_step(pid, "ensure_brd", ba_service=ba, sa_service=sa, us_service=us,
                 lld_service=lld, tc_service=tc, closure_service=cr, on_event=progress)

    recs = progress.as_run_records()
    assert recs["failure_stage"] == "hld"
    assert recs["stages"]["hld"]["state"] == FAILED

    run_failed = [e for e in progress.events if e.phase == PHASE_RUN_FAILED]
    assert len(run_failed) == 1
    assert run_failed[0].failure_stage == "hld"
    assert run_failed[0].outcome == OUTCOME_ERROR

    # NOTHING in ANY event carries the provider/exception message.
    for ev in progress.events:
        for value in ev.as_dict().values():
            assert _LEAKY_DETAIL not in str(value)
            assert "Gemini API call failed" not in str(value)


# ==========================================================================
# 9. run_id propagation
# ==========================================================================

def test_events_carry_the_active_run_id(sow_file, sample_metadata):
    progress = PipelineProgress()
    run_step("p15b_rid", "ensure_brd", sow_path=str(sow_file),
             metadata=sample_metadata, on_event=progress, **_services("p15b_rid"))

    rid = progress.run_id
    assert len(rid) == 36 and rid.count("-") == 4            # UUID4 string

    phases = [e.phase for e in progress.events]
    assert phases[0] == PHASE_RUN_STARTED
    assert phases[-1] == PHASE_RUN_COMPLETED
    assert PHASE_STAGE_STARTED in phases and PHASE_STAGE_COMPLETED in phases
    assert [e.seq for e in progress.events] == list(range(len(progress.events)))

    # every emitted event (run-level AND per-stage, incl. from the executor
    # thread) carries the SAME run_id that run_context bound.
    assert all(e.run_id == rid for e in progress.events)

    brd_started = [e for e in progress.events
                   if e.phase == PHASE_STAGE_STARTED and e.stage == "brd"]
    assert len(brd_started) == 1
    brd_completed = [e for e in progress.events
                     if e.phase == PHASE_STAGE_COMPLETED and e.stage == "brd"]
    assert len(brd_completed) == 1 and brd_completed[0].elapsed_ms is not None

    run_completed = [e for e in progress.events if e.phase == PHASE_RUN_COMPLETED][0]
    assert run_completed.pipeline_status == "awaiting_approval"
    assert run_completed.awaiting == "brd_final"
    assert run_completed.produced == {"brd": 1}


# ==========================================================================
# 10. Elapsed formatting
# ==========================================================================

@pytest.mark.parametrize("seconds, expected", [
    (0.0, "0s"),
    (0, "0s"),
    (-3, "0s"),
    (1.4, "1.4s"),
    (59.9, "59.9s"),
    (60, "1m 00s"),
    (60.0, "1m 00s"),
    (61, "1m 01s"),
    (123, "2m 03s"),
    (600, "10m 00s"),
    (3661, "61m 01s"),
    (None, "—"),
])
def test_format_elapsed(seconds, expected):
    assert format_elapsed(seconds) == expected


@pytest.mark.parametrize("ms, expected", [
    (0, "0s"), (1400, "1.4s"), (60000, "1m 00s"), (123000, "2m 03s"), (None, "—"),
])
def test_format_elapsed_ms(ms, expected):
    assert format_elapsed_ms(ms) == expected


def test_stage_labels_cover_every_stage_id_and_ids_are_unchanged():
    assert set(STAGE_LABELS) == {
        "brd", "hld", "user_stories", "lld",
        "user_story_refinement", "test_cases", "closure_report",
    }
    assert stage_label("brd") == STAGE_LABELS["brd"]
    assert stage_label("unknown_stage") == "unknown_stage"   # identity fallback
    assert stage_label(None) == ""


# ==========================================================================
# 11. Input immutability
# ==========================================================================

def test_pipeline_stage_model_never_mutates_its_inputs():
    status = _status(brd_exists=True, brd_final_version=1, us_exists=True,
                     next_step="generate_hld")
    recs = {
        "run_id": "r",
        "stages": {"brd": {"state": COMPLETED, "elapsed_ms": 1000}},
        "failure_stage": None,
    }
    status_snapshot = copy.deepcopy(status)
    recs_snapshot = copy.deepcopy(recs)

    pipeline_stage_model(status, recs)
    pipeline_stage_model(status)                 # run_records=None path too

    assert status == status_snapshot
    assert recs == recs_snapshot


# ==========================================================================
# 12. No Streamlit import / framework independence
# ==========================================================================

def test_infra_module_has_no_streamlit_and_only_stdlib_plus_status():
    import app.observability.pipeline_progress as mod

    src = Path(inspect.getfile(mod)).read_text(encoding="utf-8")
    # no import statement mentions streamlit (the docstring may name it in prose)
    assert "import streamlit" not in src
    assert "from streamlit" not in src

    tree = ast.parse(src)
    top_level_pkgs: set[str] = set()
    app_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top_level_pkgs |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_pkgs.add(node.module.split(".")[0])
            if node.module.startswith("app."):
                app_modules.add(node.module)

    assert "streamlit" not in top_level_pkgs
    assert top_level_pkgs <= {"__future__", "threading", "dataclasses", "typing", "app"}
    # the ONLY app import is the read-only next_step vocabulary
    assert app_modules == {"app.orchestration.status"}


# ==========================================================================
# ARCHITECTURAL — Phase 15B must not have altered topology / state / deps
# ==========================================================================

def test_graph_topology_declaration_is_unchanged():
    import app.orchestration.graph as gmod

    tree = ast.parse(Path(inspect.getfile(gmod)).read_text(encoding="utf-8"))
    add_node_names = [
        n.args[0].value
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "add_node" and n.args and isinstance(n.args[0], ast.Constant)
    ]
    assert add_node_names == [
        "resolve_state", "ensure_brd", "gate_brd", "ensure_hld",
        "ensure_user_stories", "gate_hld", "ensure_lld", "gate_lld",
        "ensure_test_cases", "gate_test_cases", "ensure_closure_report",
        "gate_closure_report",
    ]

    def _count(attr):
        return sum(
            1 for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr
        )

    assert _count("add_node") == 12
    assert _count("add_edge") == 8
    assert _count("add_conditional_edges") == 5


def test_compiled_graph_is_identical_with_and_without_the_callback():
    from app.agents.business_analyst.service import BusinessAnalystService
    from tests.conftest import StubBAAgent

    ba = BusinessAnalystService(project_id="p15b_topo", agent=StubBAAgent())
    g_plain = build_sdlc_graph(ba).get_graph()
    g_cb = build_sdlc_graph(ba, on_event=lambda e: None).get_graph()

    assert sorted(n.id for n in g_plain.nodes.values()) == \
        sorted(n.id for n in g_cb.nodes.values())
    assert sorted((e.source, e.target) for e in g_plain.edges) == \
        sorted((e.source, e.target) for e in g_cb.edges)


def test_sdlcstate_field_set_is_unchanged():
    from app.orchestration.state import SDLCState

    assert set(SDLCState.__annotations__) == {
        "project_id", "sow_path", "metadata", "request",
        "brd_latest_version", "brd_final_version",
        "hld_latest_version", "hld_final_version", "us_latest_version",
        "lld_latest_version", "lld_final_version",
        "tc_latest_version", "tc_final_version",
        "closure_latest_version", "closure_final_version",
        "produced", "status", "awaiting",
    }


def test_no_new_dependency_was_introduced():
    repo_root = Path(__file__).resolve().parents[1]
    reqs = (repo_root / "requirements.txt").read_text(encoding="utf-8")
    pkgs = {
        line.split("==")[0].strip().lower()
        for line in reqs.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert pkgs == {
        "streamlit", "langchain", "langchain-google-genai", "langgraph",
        "python-docx", "pymupdf", "pydantic", "pydantic-settings",
        "python-dotenv", "pytest",
    }
