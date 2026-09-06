"""
Phase 11B — HLD and Initial User Story generation now fan out CONCURRENTLY from
`gate_brd` and fan back in at `gate_hld` (no explicit join node).

    gate_brd(complete) -> [ ensure_hld , ensure_user_stories ] -> gate_hld -> END

These tests pin the new fan-out/fan-in behaviour, the `produced` reducer, and
the recoverable-partial-state guarantee. Deterministic: BA / SA / US LLMs are
stubs injected via `agent=` / `*_service=`. No Gemini calls.
"""

import pytest

import app.orchestration.graph as graph_mod
from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.solution_architect.agent import SolutionArchitectAgentError
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.test_case.service import TestCaseService
from app.orchestration.graph import run_step
from app.orchestration.state import _merge_produced
from tests.conftest import (
    StubBAAgent,
    StubLLDAgent,
    StubSAAgent,
    StubTestCaseAgent,
    StubUserStoryAgent,
)

PID = "sdlc11b"


def _svcs(pid=PID, *, sa_agent=None, us_agent=None):
    ba = BusinessAnalystService(project_id=pid, agent=StubBAAgent())
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=sa_agent or StubSAAgent())
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=us_agent or StubUserStoryAgent())
    lld = LowLevelDesignService(project_id=pid, sa_service=sa, ba_service=ba, agent=StubLLDAgent())
    tc = TestCaseService(project_id=pid, agent=StubTestCaseAgent())
    return ba, sa, us, lld, tc


def _final_brd(ba, sow_file, sample_metadata):
    ba.generate_initial_brd(str(sow_file), sample_metadata)
    ba.choose_final_brd(1)


def _run(pid, ba, sa, us, lld, tc, sow_file, sample_metadata):
    return run_step(
        pid, "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
        ba_service=ba, sa_service=sa, us_service=us,
        lld_service=lld, tc_service=tc,
    )


# --- A. fan-out + fan-in --------------------------------------------------

def test_hld_and_us_fan_out_and_join(sow_file, sample_metadata, monkeypatch):
    ba, sa, us, lld, tc = _svcs()
    _final_brd(ba, sow_file, sample_metadata)

    # Spy on the fan-in node: it must run EXACTLY ONCE, after both branches.
    calls = {"gate_hld": 0}
    _orig_gate_hld = graph_mod._gate_hld_node

    def _spy_gate_hld(state):
        calls["gate_hld"] += 1
        return _orig_gate_hld(state)

    monkeypatch.setattr(graph_mod, "_gate_hld_node", _spy_gate_hld)

    s = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)

    assert s["produced"] == {"hld": 1, "us": 1}          # both branches, merged
    assert s["status"] == "awaiting_approval"
    assert s["awaiting"] == "hld_final"                   # HLD approval gate unchanged
    assert [v.version for v in sa.get_all_versions()] == [1]   # HLD stream: exactly v1
    assert [v.version for v in us.get_all_versions()] == [1]   # US stream: exactly v1
    assert calls["gate_hld"] == 1                         # fan-in ran once, not per branch
    # neither branch was finalized by the graph
    assert sa.get_final_hld() is None
    assert not any(v.is_final for v in us.get_all_versions())


def test_second_run_after_fan_out_is_a_no_op(sow_file, sample_metadata):
    ba, sa, us, lld, tc = _svcs()
    _final_brd(ba, sow_file, sample_metadata)

    _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)
    s2 = _run(PID, ba, sa, us, lld, tc, sow_file, sample_metadata)

    assert s2["produced"] == {}                           # idempotency guards hold
    assert [v.version for v in sa.get_all_versions()] == [1]
    assert [v.version for v in us.get_all_versions()] == [1]


# --- B. the produced reducer -------------------------------------------

def test_produced_reducer_merges_disjoint_branch_keys():
    assert _merge_produced({}, {"hld": 1}) == {"hld": 1}
    assert _merge_produced({"hld": 1}, {"us": 1}) == {"hld": 1, "us": 1}
    # order-independent for disjoint keys (the fan-out completes in any order)
    assert _merge_produced({"us": 1}, {"hld": 1}) == {"hld": 1, "us": 1}
    # None-safe on either side
    assert _merge_produced(None, {"us": 1}) == {"us": 1}
    assert _merge_produced({"hld": 1}, None) == {"hld": 1}
    assert _merge_produced(None, None) == {}
    # inputs are not mutated
    a, b = {"hld": 1}, {"us": 1}
    _merge_produced(a, b)
    assert a == {"hld": 1} and b == {"us": 1}


# --- C. failure of one branch is recoverable on re-run ----------------

def test_parallel_branch_failure_is_recoverable(sow_file, sample_metadata):
    class _BoomSA:
        def generate_hld(self, brd_text, metadata):
            raise SolutionArchitectAgentError("HLD Gemini exploded")

    ba, _, us, lld, tc = _svcs()
    _final_brd(ba, sow_file, sample_metadata)

    # Run 1: HLD branch fails. The error propagates out of run_step. The US
    # branch may or may not have persisted its v1 (concurrent, timing-dependent).
    sa_boom = SolutionArchitectService(project_id=PID, ba_service=ba, agent=_BoomSA())
    with pytest.raises(SolutionArchitectAgentError):
        _run(PID, ba, sa_boom, us, lld, tc, sow_file, sample_metadata)
    assert sa_boom.get_all_versions() == []               # nothing persisted for HLD

    # Run 2: same project, working SA agent. resolve_state re-derives pointers
    # from persistence, so whichever branch already committed no-ops.
    sa_ok = SolutionArchitectService(project_id=PID, ba_service=ba, agent=StubSAAgent())
    s2 = _run(PID, ba, sa_ok, us, lld, tc, sow_file, sample_metadata)

    assert [v.version for v in sa_ok.get_all_versions()] == [1]   # HLD generated, once
    assert [v.version for v in us.get_all_versions()] == [1]      # US: exactly one, no duplicate
    assert s2["status"] == "awaiting_approval" and s2["awaiting"] == "hld_final"
