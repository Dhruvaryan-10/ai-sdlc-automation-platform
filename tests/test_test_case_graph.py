"""
Phase 8A — QA-only LangGraph orchestration pilot tests.

Proves that constructing `TestCaseService(..., use_graph=True)` runs the SAME QA
generate / refine flow through a small LangGraph workflow
(START -> prepare -> invoke_agent -> persist -> END) with byte-for-byte the same
externally visible behaviour as the default (`use_graph=False`) path.

Deterministic: the QA LLM is a stub injected via `agent=`; the only exception is
two tests that use a real `TestCaseAgent(structured=True)` with a *fake*
structured LLM (still no network) to prove structured output + retry survive the
graph. No Gemini calls anywhere.
"""

import json

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.graph import (
    QAState,
    build_qa_graph,
    run_qa,
)
from app.agents.test_case.service import (
    NoFinalBRDError,
    TestCaseLockedError,
    TestCaseService,
)

PID = "graphproj"


# --- upstream setup (real Phase 1-5 services + stub agents) ----------------

def _final_brd(stub_ba_agent, sow_file, sample_metadata, pid=PID):
    ba = BusinessAnalystService(project_id=pid, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    return ba


def _final_hld(ba, stub_sa_agent, pid=PID):
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    sa.choose_final_hld(1)
    return sa


def _final_lld(sa, ba, stub_lld_agent, pid=PID):
    lld = LowLevelDesignService(project_id=pid, sa_service=sa, ba_service=ba, agent=stub_lld_agent)
    lld.generate_initial_lld()
    lld.choose_final_lld(1)
    return lld


def _stories(ba, stub_us_agent, pid=PID, *, finalize=True):
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()
    if finalize:
        us.choose_final_stories(1)
    return us


# --- 1. graph structure -------------------------------------------------

def test_graph_structure_is_the_approved_linear_pipeline(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    compiled = build_qa_graph(svc)
    g = compiled.get_graph()

    assert set(g.nodes) == {"__start__", "prepare", "invoke_agent", "persist", "__end__"}
    assert sorted((e.source, e.target) for e in g.edges) == [
        ("__start__", "prepare"),
        ("invoke_agent", "persist"),
        ("persist", "__end__"),
        ("prepare", "invoke_agent"),
    ]
    assert type(compiled).__name__ == "CompiledStateGraph"


# --- 2. state construction -------------------------------------------

def test_qa_state_declares_the_expected_keys():
    keys = set(QAState.__annotations__)
    assert {
        "mode", "feedback", "brd", "hld", "lld", "us", "metadata",
        "current_test_cases", "current_version", "raw_json", "version",
    } == keys


def test_prepare_node_populates_resolved_inputs(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    from app.agents.test_case.graph import _make_prepare_node
    out = _make_prepare_node(svc)({"mode": "generate"})

    assert out["brd"].version == 1 and out["brd"].is_final is True
    assert out["hld"] is None and out["lld"] is None and out["us"] is None
    assert out["metadata"].project_name  # derived from the BRD header
    assert out["current_test_cases"] is None and out["current_version"] is None


def test_invoke_agent_node_returns_raw_json_from_the_agent(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    from app.agents.test_case.graph import _make_invoke_agent_node, _make_prepare_node
    state = {"mode": "generate", "feedback": None}
    state.update(_make_prepare_node(svc)(state))
    out = _make_invoke_agent_node(svc)(state)

    parsed = json.loads(out["raw_json"])
    assert [c["id"] for c in parsed["test_cases"]] == ["TC-001", "TC-002"]
    assert stub_tc_agent.generate_calls  # the existing agent was invoked


def test_persist_node_appends_a_version(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    from app.agents.test_case.graph import (
        _make_invoke_agent_node,
        _make_persist_node,
        _make_prepare_node,
    )
    state = {"mode": "generate", "feedback": None}
    state.update(_make_prepare_node(svc)(state))
    state.update(_make_invoke_agent_node(svc)(state))
    out = _make_persist_node(svc)(state)

    v = out["version"]
    assert v.version == 1 and v.source == "initial"
    assert "## TC-001 — " in v.content
    assert v.source_ref == "brd_v1;hld_vnone;lld_vnone;us_vnone"


# --- 3. generate execution through the graph -----------------------

def test_generate_through_graph_creates_v1(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    v1 = svc.generate()

    assert v1.version == 1 and v1.source == "initial"
    assert "## TC-001 — " in v1.content and "## TC-002 — " in v1.content
    assert "**Version:** 1" in v1.content
    assert "**Source:** Generated from artifacts" in v1.content
    assert v1.source_ref == "brd_v1;hld_vnone;lld_vnone;us_vnone"
    assert svc.get_all_versions()[-1].version == 1


def test_regenerate_through_graph_appends_v2(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    svc.generate()
    v2 = svc.regenerate()   # delegates to generate(), still graph-routed

    assert v2.version == 2 and v2.source == "ai_refine"
    assert [x.version for x in svc.get_all_versions()] == [1, 2]


def test_generate_through_graph_uses_optional_context_when_present(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, stub_us_agent, stub_tc_agent,
    sow_file, sample_metadata,
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _final_hld(ba, stub_sa_agent)
    _final_lld(sa, ba, stub_lld_agent)
    _stories(ba, stub_us_agent)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    v1 = svc.generate()

    assert v1.source_ref == "brd_v1;hld_v1;lld_v1;us_v1"
    assert "**Built From:** BRD v1, HLD v1, LLD v1, User Stories v1" in v1.content
    # the agent saw real artifact text, not sentinels
    _, hld_text, lld_text, us_text, _ = stub_tc_agent.generate_calls[-1]
    assert "(no accepted HLD available)" not in hld_text
    assert "(no accepted LLD available)" not in lld_text
    assert "(no user stories available)" not in us_text


# --- 4. refine execution through the graph -------------------------

def test_refine_through_graph_appends_ai_refine_version(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    svc.generate()
    v2 = svc.refine_with_ai("Tighten TC-002 wording")

    assert v2.version == 2 and v2.source == "ai_refine"
    assert "**Source:** Artifact-refined" in v2.content
    assert v2.note.startswith("Refined from BRD v1")
    # every existing TC-NNN id preserved (stub tweaks only TC-002's title)
    assert "## TC-001 — " in v2.content and "## TC-002 — " in v2.content
    assert stub_tc_agent.refine_calls


def test_refine_through_graph_passes_current_doc_and_version(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    v1 = svc.generate()
    svc.refine_with_ai("feedback text")

    call = stub_tc_agent.refine_calls[-1]
    current_test_cases, feedback, current_version = call[0], call[1], call[2]
    assert current_test_cases == v1.content
    assert current_version == 1
    assert feedback == "feedback text"


# --- 5. direct-vs-graph parity ------------------------------------

def test_direct_and_graph_generate_produce_identical_versions(
    stub_ba_agent, stub_tc_agent, sow_file, sample_metadata,
):
    # one project per path so both versions are v1
    _final_brd(stub_ba_agent, sow_file, sample_metadata, pid="parity_direct")
    _final_brd(stub_ba_agent, sow_file, sample_metadata, pid="parity_graph")

    direct = TestCaseService(project_id="parity_direct", agent=stub_tc_agent).generate()
    graphed = TestCaseService(
        project_id="parity_graph", agent=stub_tc_agent, use_graph=True
    ).generate()

    assert graphed.content == direct.content
    assert graphed.source == direct.source
    assert graphed.source_ref == direct.source_ref
    assert graphed.note == direct.note
    assert graphed.version == direct.version == 1


def test_direct_and_graph_refine_produce_identical_versions(
    stub_ba_agent, stub_tc_agent, sow_file, sample_metadata,
):
    _final_brd(stub_ba_agent, sow_file, sample_metadata, pid="rparity_direct")
    _final_brd(stub_ba_agent, sow_file, sample_metadata, pid="rparity_graph")

    d = TestCaseService(project_id="rparity_direct", agent=stub_tc_agent)
    g = TestCaseService(project_id="rparity_graph", agent=stub_tc_agent, use_graph=True)
    d.generate()
    g.generate()

    d2 = d.refine_with_ai("same feedback")
    g2 = g.refine_with_ai("same feedback")

    assert g2.content == d2.content
    assert g2.source == d2.source == "ai_refine"
    assert g2.source_ref == d2.source_ref
    assert g2.note == d2.note
    assert g2.version == d2.version == 2


# --- 6. failure propagation --------------------------------------

def test_missing_final_brd_propagates_no_final_brd_error(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)  # NOT finalised
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    with pytest.raises(NoFinalBRDError):
        svc.generate()
    assert svc.has_versions() is False


def test_locked_final_propagates_test_case_locked_error(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)
    svc.generate()
    svc.choose_final(1)  # marks final + locks

    with pytest.raises(TestCaseLockedError):
        svc.generate()
    with pytest.raises(TestCaseLockedError):
        svc.refine_with_ai("x")


def test_refine_with_no_existing_version_propagates_value_error(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    with pytest.raises(ValueError, match="No existing test cases"):
        svc.refine_with_ai("feedback")


def test_bad_agent_json_propagates_value_error_through_graph(stub_ba_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)

    class _BadAgent:
        def generate_test_cases(self, **kw):
            return "not json at all"

    svc = TestCaseService(project_id=PID, agent=_BadAgent(), use_graph=True)
    with pytest.raises(ValueError):
        svc.generate()
    assert svc.has_versions() is False


# --- 7. structured-output preservation --------------------------

def test_structured_output_still_runs_under_the_graph(stub_ba_agent, sow_file, sample_metadata, monkeypatch):
    from app.agents.test_case.agent import TestCaseAgent
    from app.agents.test_case.schema import TestCaseList
    from tests.conftest import STUB_TEST_CASES

    _final_brd(stub_ba_agent, sow_file, sample_metadata)

    class _FakeStructuredLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, prompt):
            self.calls += 1
            return TestCaseList(**STUB_TEST_CASES)

    agent = TestCaseAgent(structured=True)  # no network at construction
    fake = _FakeStructuredLLM()
    monkeypatch.setattr(agent, "_structured_llm", fake)

    svc = TestCaseService(project_id=PID, agent=agent, use_graph=True)
    v1 = svc.generate()

    assert fake.calls == 1                      # the structured path was used
    assert v1.version == 1 and "## TC-001 — " in v1.content
    assert v1.source_ref == "brd_v1;hld_vnone;lld_vnone;us_vnone"


# --- 8. retry preservation ------------------------------------

def test_transient_retry_still_works_under_the_graph(stub_ba_agent, sow_file, sample_metadata, monkeypatch):
    from app.agents.test_case.agent import TestCaseAgent
    from app.agents.test_case.schema import TestCaseList
    from tests.conftest import STUB_TEST_CASES

    slept: list[float] = []
    monkeypatch.setattr("app.agents.test_case.agent.time.sleep", lambda s: slept.append(s))
    _final_brd(stub_ba_agent, sow_file, sample_metadata)

    class _Transient503(Exception):
        def __str__(self):
            return "503 UNAVAILABLE: model overloaded, high demand"

    class _FlakyLLM:
        def __init__(self):
            self.calls = 0

        def invoke(self, prompt):
            self.calls += 1
            if self.calls == 1:
                raise _Transient503()
            return TestCaseList(**STUB_TEST_CASES)

    agent = TestCaseAgent(structured=True)
    flaky = _FlakyLLM()
    monkeypatch.setattr(agent, "_structured_llm", flaky)

    svc = TestCaseService(project_id=PID, agent=agent, use_graph=True)
    v1 = svc.generate()

    assert flaky.calls == 2          # one transient failure, one success
    assert len(slept) == 1          # one backoff sleep, inside the agent
    assert v1.version == 1


# --- 9. persistence / version compatibility --------------------

def test_graph_persists_the_same_record_shape_and_stream(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata, isolated_output_dir):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True)

    svc.generate()
    svc.regenerate()

    versions_file = isolated_output_dir / PID / "test_cases" / "versions.json"
    assert versions_file.exists()
    records = json.loads(versions_file.read_text(encoding="utf-8"))
    assert [r["version"] for r in records] == [1, 2]
    assert set(records[0]) == {
        "version", "content", "source", "created_at", "note", "is_final",
        "is_locked", "source_ref",
    }
    # finalization / lock still work on top of a graph-produced stream
    svc.choose_final(1)
    assert svc.is_locked() is True
    svc.unlock_final()
    assert svc.is_locked() is False and svc.get_final() is not None


# --- 10. isolation from other artifact streams -----------------

def test_graph_generation_touches_only_the_test_cases_stream(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, stub_us_agent, stub_tc_agent,
    sow_file, sample_metadata, isolated_output_dir,
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _final_hld(ba, stub_sa_agent)
    _final_lld(sa, ba, stub_lld_agent)
    _stories(ba, stub_us_agent)

    proj = isolated_output_dir / PID
    before = {
        name: (proj / name / "versions.json").read_bytes()
        for name in ("hld", "lld", "user_stories")
    }
    before_brd = (proj / "versions.json").read_bytes()

    TestCaseService(project_id=PID, agent=stub_tc_agent, use_graph=True).generate()

    for name, blob in before.items():
        assert (proj / name / "versions.json").read_bytes() == blob, f"{name} stream changed"
    assert (proj / "versions.json").read_bytes() == before_brd
    assert (proj / "test_cases" / "versions.json").exists()


def test_graph_module_imports_no_other_agent_package():
    import ast
    from pathlib import Path

    src = Path("app/agents/test_case/graph.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)

    forbidden = ("solution_architect", "initial_user_story",
                 "low_level_design", "user_story_refinement")
    assert not any(f in mod for mod in imported for f in forbidden)
    ba_imports = [m for m in imported if m.startswith("app.agents.business_analyst")]
    assert ba_imports == ["app.agents.business_analyst.agent"]  # only ProjectMetadata


# --- default path unchanged --------------------------------------

def test_use_graph_defaults_to_false_and_keeps_the_inline_path(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent)  # no use_graph
    assert svc._use_graph is False
    v1 = svc.generate()
    assert v1.version == 1 and v1.source_ref == "brd_v1;hld_vnone;lld_vnone;us_vnone"
