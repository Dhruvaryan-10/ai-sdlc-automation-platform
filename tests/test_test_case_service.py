"""
QA / Test Case service tests — the Phase 6 acceptance checklist (items 1-27).

Deterministic; the QA LLM is a stub injected via `agent=`. Upstream artifact
streams (BRD / HLD / LLD / User Stories) are populated with the real Phase 1-5
services + their stub agents, purely as test setup — the test_case package
imports none of them.
"""

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import (
    NoFinalBRDError,
    TestCaseLockedError,
    TestCaseService,
)

PID = "proj"


# --- setup helpers --------------------------------------------------------

def _final_brd(stub_ba_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    return ba


def _final_hld(ba, stub_sa_agent):
    sa = SolutionArchitectService(project_id=PID, ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    sa.choose_final_hld(1)
    return sa


def _final_lld(sa, ba, stub_lld_agent):
    lld = LowLevelDesignService(project_id=PID, sa_service=sa, ba_service=ba, agent=stub_lld_agent)
    lld.generate_initial_lld()
    lld.choose_final_lld(1)
    return lld


def _stories(ba, stub_us_agent, *, finalize=False):
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()
    if finalize:
        us.choose_final_stories(1)
    return us


def _qa(stub_tc_agent):
    return TestCaseService(project_id=PID, agent=stub_tc_agent)


def _tc_section(content: str, tc_id: str) -> str:
    """The '## {tc_id} — ...' block of a rendered test-case document."""
    lines = content.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"## {tc_id} "))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).rstrip()


# --- TEST 1: missing BRD blocks generation ------------------------------

def test_missing_brd_blocks_generation(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)  # NOT finalised
    with pytest.raises(NoFinalBRDError):
        _qa(stub_tc_agent).generate()
    assert _qa(stub_tc_agent).has_versions() is False


# --- TEST 2 + 9 + 10: BRD only -> generation succeeds, v1, stable TC ids ---

def test_brd_only_generation_succeeds(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    qa = _qa(stub_tc_agent)

    v1 = qa.generate()

    assert v1.version == 1
    assert v1.source == "initial"
    assert qa.has_versions() is True and len(qa.get_all_versions()) == 1
    assert "## TC-001 — " in v1.content and "## TC-002 — " in v1.content
    assert "**Version:** 1" in v1.content
    assert "**Source:** Generated from artifacts" in v1.content


# --- TEST 3/4/5: missing HLD / LLD / User Stories do NOT block -----------

def test_missing_optional_artifacts_do_not_block(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    qa = _qa(stub_tc_agent)

    v1 = qa.generate()

    _, hld_text, lld_text, us_text, _ = stub_tc_agent.generate_calls[0]
    assert hld_text == "(no accepted HLD available)"
    assert lld_text == "(no accepted LLD available)"
    assert us_text == "(no user stories available)"
    assert v1.source_ref == "brd_v1;hld_vnone;lld_vnone;us_vnone"
    assert "HLD unavailable" in v1.content and "LLD unavailable" in v1.content
    assert "User Stories unavailable" in v1.content
    assert "(HLD, LLD, User Stories: unavailable)" in v1.note


# --- TEST 6/7/8 + 26: optional artifacts incorporated + provenance ------

def test_optional_artifacts_are_incorporated_as_context(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, stub_us_agent, stub_tc_agent,
    sow_file, sample_metadata,
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _stories(ba, stub_us_agent, finalize=True)
    sa = _final_hld(ba, stub_sa_agent)
    _final_lld(sa, ba, stub_lld_agent)

    qa = _qa(stub_tc_agent)
    v1 = qa.generate()

    _, hld_text, lld_text, us_text, _ = stub_tc_agent.generate_calls[0]
    assert "High-Level Design" in hld_text
    assert "Low-Level Design" in lld_text
    assert "US-001" in us_text
    assert v1.source_ref == "brd_v1;hld_v1;lld_v1;us_v1"
    assert qa.recorded_source_versions() == {"brd": 1, "hld": 1, "lld": 1, "us": 1}
    assert "**Built From:** BRD v1, HLD v1, LLD v1, User Stories v1" in v1.content


def test_user_stories_context_prefers_final_then_latest(
    stub_ba_agent, stub_us_agent, stub_tc_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    us = _stories(ba, stub_us_agent)          # v1 draft only, not finalised
    us.save_manual_edit(us.get_version(1).content + "\nextra\n")  # v2 latest

    qa = _qa(stub_tc_agent)
    qa.generate()
    assert qa.recorded_source_versions()["us"] == 2   # latest used when no final exists

    us.choose_final_stories(1)                # now v1 is final
    qa.regenerate()
    assert qa.recorded_source_versions()["us"] == 1   # final preferred over latest


# --- TEST 11 + 13: manual edit -> new version, prior versions unchanged --

def test_manual_edit_creates_new_version(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    qa = _qa(stub_tc_agent)
    v1 = qa.generate()
    v1_content = v1.content

    v2 = qa.save_manual_edit(v1_content + "\n\n## TC-009 — Hand added\n**Expected Result:** ok\n")

    assert v2.version == 2
    assert v2.source == "manual_edit"
    assert qa.get_version(1).content == v1_content
    assert "TC-009" in qa.get_version(2).content


# --- TEST 12 + 24 + 25: AI refine -> new version, ids stable, unaffected preserved --

def test_ai_refine_preserves_ids_and_unaffected_cases(
    stub_ba_agent, stub_tc_agent, sow_file, sample_metadata
):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    qa = _qa(stub_tc_agent)
    v1 = qa.generate()

    v2 = qa.refine_with_ai("tighten the invalid-email message")

    assert v2.version == 2
    assert v2.source == "ai_refine"
    # every TC id from v1 still present in v2
    assert "## TC-001 — " in v2.content and "## TC-002 — " in v2.content
    # TC-001 (unaffected) is byte-identical between v1 and v2
    assert _tc_section(v1.content, "TC-001") == _tc_section(v2.content, "TC-001")
    # TC-002 (the one the stub changed) differs
    assert _tc_section(v1.content, "TC-002") != _tc_section(v2.content, "TC-002")
    assert "tighten the invalid-email message" in v2.content


def test_history_is_append_only(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    qa = _qa(stub_tc_agent)
    qa.generate()
    snap = [v.model_dump() for v in qa.get_all_versions()]

    qa.save_manual_edit(qa.get_version(1).content + "\n\n## TC-050 — x\n**Expected Result:** ok\n")
    qa.refine_with_ai("add negative cases")

    after = qa.get_all_versions()
    assert [v.version for v in after] == [1, 2, 3]
    assert [v.model_dump() for v in after][:1] == snap  # v1 entry untouched


# --- TEST 14 + 15 + 16: final / lock / unlock -------------------------

def test_lock_blocks_edit_and_refine_unlock_restores(
    stub_ba_agent, stub_tc_agent, sow_file, sample_metadata
):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    qa = _qa(stub_tc_agent)
    qa.generate()
    qa.choose_final(1)

    assert qa.is_locked() is True
    with pytest.raises(TestCaseLockedError):
        qa.save_manual_edit("## TC-001 — x\n**Expected Result:** y\n")
    with pytest.raises(TestCaseLockedError):
        qa.refine_with_ai("whatever")

    qa.unlock_final()
    assert qa.is_locked() is False
    v2 = qa.save_manual_edit(qa.get_version(1).content + "\n\n## TC-060 — z\n**Expected Result:** ok\n")
    assert v2.version == 2
    assert qa.get_final().version == 1 and qa.get_final().is_final is True
    assert len(qa.get_all_versions()) == 2


# --- TEST 17/18/19/20: per-source staleness ---------------------------

def _refined_stack(stub_ba_agent, stub_sa_agent, stub_lld_agent, stub_us_agent, stub_tc_agent,
                   sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    us = _stories(ba, stub_us_agent, finalize=True)
    sa = _final_hld(ba, stub_sa_agent)
    lld = _final_lld(sa, ba, stub_lld_agent)
    qa = _qa(stub_tc_agent)
    qa.generate()  # brd_v1;hld_v1;lld_v1;us_v1
    return ba, sa, lld, us, qa


def test_brd_change_marks_stale(stub_ba_agent, stub_sa_agent, stub_lld_agent, stub_us_agent,
                                stub_tc_agent, sow_file, sample_metadata):
    ba, sa, lld, us, qa = _refined_stack(stub_ba_agent, stub_sa_agent, stub_lld_agent,
                                         stub_us_agent, stub_tc_agent, sow_file, sample_metadata)
    snap = [v.model_dump() for v in qa.get_all_versions()]
    assert qa.is_stale() is False

    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\n\nNew requirement.\n")
    ba.choose_final_brd(2)

    assert qa.stale_sources() == ["BRD"]
    assert [v.model_dump() for v in qa.get_all_versions()] == snap  # not mutated


def test_hld_change_marks_stale_when_used(stub_ba_agent, stub_sa_agent, stub_lld_agent,
                                          stub_us_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, lld, us, qa = _refined_stack(stub_ba_agent, stub_sa_agent, stub_lld_agent,
                                         stub_us_agent, stub_tc_agent, sow_file, sample_metadata)
    sa.unlock_final_hld()
    sa.save_manual_edit(sa.get_version(1).content + "\n\nNew component.\n")
    sa.choose_final_hld(2)
    assert qa.stale_sources() == ["HLD"]


def test_lld_change_marks_stale_when_used(stub_ba_agent, stub_sa_agent, stub_lld_agent,
                                          stub_us_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, lld, us, qa = _refined_stack(stub_ba_agent, stub_sa_agent, stub_lld_agent,
                                         stub_us_agent, stub_tc_agent, sow_file, sample_metadata)
    lld.unlock_final_lld()
    lld.save_manual_edit(lld.get_version(1).content + "\n\nExtra table.\n")
    lld.choose_final_lld(2)
    assert qa.stale_sources() == ["LLD"]


def test_user_story_change_marks_stale_when_used(stub_ba_agent, stub_sa_agent, stub_lld_agent,
                                                 stub_us_agent, stub_tc_agent, sow_file, sample_metadata):
    ba, sa, lld, us, qa = _refined_stack(stub_ba_agent, stub_sa_agent, stub_lld_agent,
                                         stub_us_agent, stub_tc_agent, sow_file, sample_metadata)
    # _refined_stack finalised user stories at v1; unlock, add v2, re-finalise at v2
    us.unlock_final_stories()
    us.save_manual_edit(us.get_version(1).content + "\nedit\n")   # v2 latest
    us.choose_final_stories(2)                                    # final now v2
    assert qa.stale_sources() == ["User Stories"]


# --- TEST 3-related: previously-absent optional artifact is NOT stale ---

def test_absent_then_present_optional_is_not_stale(
    stub_ba_agent, stub_sa_agent, stub_tc_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    qa = _qa(stub_tc_agent)
    qa.generate()  # no HLD -> hld recorded as None
    assert qa.recorded_source_versions()["hld"] is None
    assert qa.is_stale() is False

    _final_hld(ba, stub_sa_agent)  # a final HLD appears afterwards

    assert qa.stale_sources() == []   # newly-appeared optional artifact is NOT stale


# --- TEST 21 + 22: no automatic regeneration; explicit action creates a version ---

def test_staleness_does_not_regenerate_and_explicit_action_does(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, stub_us_agent, stub_tc_agent,
    sow_file, sample_metadata,
):
    ba, sa, lld, us, qa = _refined_stack(stub_ba_agent, stub_sa_agent, stub_lld_agent,
                                         stub_us_agent, stub_tc_agent, sow_file, sample_metadata)
    before = [v.model_dump() for v in qa.get_all_versions()]
    gen_calls = len(stub_tc_agent.generate_calls)

    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\nx\n")
    ba.choose_final_brd(2)

    _ = qa.is_stale()  # merely checking must not trigger anything
    assert [v.model_dump() for v in qa.get_all_versions()] == before
    assert len(stub_tc_agent.generate_calls) == gen_calls

    v2 = qa.regenerate()  # explicit
    assert v2.version == 2
    assert qa.is_stale() is False
    assert qa.recorded_source_versions()["brd"] == 2


# --- TEST 23: generation writes only the test_cases stream ------------

def test_generation_writes_only_test_case_stream(
    isolated_output_dir, stub_ba_agent, stub_sa_agent, stub_lld_agent, stub_us_agent,
    stub_tc_agent, sow_file, sample_metadata,
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _stories(ba, stub_us_agent, finalize=True)
    sa = _final_hld(ba, stub_sa_agent)
    _final_lld(sa, ba, stub_lld_agent)

    brd_before = (isolated_output_dir / PID / "versions.json").read_text()
    hld_before = (isolated_output_dir / PID / "hld" / "versions.json").read_text()
    lld_before = (isolated_output_dir / PID / "lld" / "versions.json").read_text()
    us_before = (isolated_output_dir / PID / "user_stories" / "versions.json").read_text()

    _qa(stub_tc_agent).generate()

    assert (isolated_output_dir / PID / "test_cases" / "versions.json").exists()
    assert (isolated_output_dir / PID / "versions.json").read_text() == brd_before
    assert (isolated_output_dir / PID / "hld" / "versions.json").read_text() == hld_before
    assert (isolated_output_dir / PID / "lld" / "versions.json").read_text() == lld_before
    assert (isolated_output_dir / PID / "user_stories" / "versions.json").read_text() == us_before


# --- TEST 27: optional-missing artifacts represented correctly in provenance --

def test_provenance_marks_missing_optional_artifacts(
    stub_ba_agent, stub_sa_agent, stub_tc_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _final_hld(ba, stub_sa_agent)  # HLD present; LLD + US absent
    qa = _qa(stub_tc_agent)
    v1 = qa.generate()

    assert v1.source_ref == "brd_v1;hld_v1;lld_vnone;us_vnone"
    assert qa.recorded_source_versions() == {"brd": 1, "hld": 1, "lld": None, "us": None}
    assert "**Built From:** BRD v1, HLD v1, LLD unavailable, User Stories unavailable" in v1.content
    assert "(LLD, User Stories: unavailable)" in v1.note


def test_metadata_derived_from_brd_header(stub_ba_agent, stub_tc_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    qa = _qa(stub_tc_agent)
    v1 = qa.generate()
    assert v1.content.startswith("# Test Project — Test Cases")
    assert "**Client:** Acme Corp" in v1.content
    assert "**Project Type:** Web Application" in v1.content


def test_invalid_json_from_agent_is_reported(stub_ba_agent, sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)

    class BadAgent:
        def generate_test_cases(self, **kw):
            return "not json at all"

    qa = TestCaseService(project_id=PID, agent=BadAgent())
    with pytest.raises(ValueError):
        qa.generate()


# --- R1/R2: test steps render as a clean single ordered list ----------------

def test_generated_test_steps_are_a_clean_ordered_list(
    stub_ba_agent, stub_tc_agent, sow_file, sample_metadata
):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    v1 = _qa(stub_tc_agent).generate()

    steps_block = _tc_section(v1.content, "TC-001")
    assert "1. Open the registration page." in steps_block
    assert "2. Enter valid details." in steps_block
    assert "3. Submit the form." in steps_block
    # no bullet-wrapped steps, no doubled numbering
    assert "- 1. " not in v1.content and "- 2. " not in v1.content
    assert "1. 1. " not in v1.content


def test_render_markdown_strips_stray_step_numbers():
    cases = [{
        "id": "TC-001", "title": "Doubled numbers",
        "requirement_or_story_ref": "FR-1",
        "test_steps": ["1. First action", "2) Second action", "3 ) Third action"],
        "expected_result": "ok", "priority": "High", "test_type": "Functional",
    }]
    md = TestCaseService._render_markdown(
        cases, project_name="P", client_name="C", project_type="T",
        version=1, source_label="Generated from artifacts",
        built_from="BRD v1, HLD unavailable, LLD unavailable, User Stories unavailable",
    )
    assert "1. First action" in md
    assert "2. Second action" in md
    assert "3. Third action" in md
    for bad in ("1. 1.", "2. 2)", "3. 3 )", "- 1.", "- 2)"):
        assert bad not in md


# --- R5: non-blocking traceability warning ---------------------------------

import logging as _logging

_SVC_LOGGER = "app.agents.test_case.service"


def _capture_service_warnings(monkeypatch, caplog):
    """The app logger sets propagate=False; re-enable it so caplog can see it."""
    monkeypatch.setattr(_logging.getLogger(_SVC_LOGGER), "propagate", True)
    return caplog.at_level("WARNING", logger=_SVC_LOGGER)


def test_ungrounded_reference_logs_warning_but_still_generates(
    stub_ba_agent, sow_file, sample_metadata, caplog, monkeypatch
):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)

    class RefAgent:
        def generate_test_cases(self, **kw):
            import json as _json
            return _json.dumps({"test_cases": [{
                "id": "TC-001", "title": "Cites a missing FR",
                "requirement_or_story_ref": "FR-999",
                "brd_reference": "FR-999",
                "test_steps": ["Do something"], "expected_result": "ok",
                "priority": "High", "test_type": "Functional",
            }]})

    qa = TestCaseService(project_id=PID, agent=RefAgent())
    with _capture_service_warnings(monkeypatch, caplog):
        v1 = qa.generate()

    assert v1.version == 1                       # generation NOT blocked
    assert "## TC-001 — " in v1.content          # test case NOT discarded
    assert any("Traceability" in r.message and "FR-999" in r.message
               for r in caplog.records)


def test_grounded_reference_produces_no_traceability_warning(
    stub_ba_agent, stub_tc_agent, sow_file, sample_metadata, caplog, monkeypatch
):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)
    with _capture_service_warnings(monkeypatch, caplog):
        _qa(stub_tc_agent).generate()
    assert not any("Traceability" in r.message for r in caplog.records)


# --- LangChain structured-output pilot: end-to-end through the real agent ----

def test_structured_output_path_feeds_service_unchanged(
    stub_ba_agent, sow_file, sample_metadata, monkeypatch
):
    """A real TestCaseAgent on the structured path (fake structured LLM) still
    produces a JSON string TestCaseService validates, renders and persists."""
    from app.agents.test_case.agent import TestCaseAgent
    from app.agents.test_case.schema import TestCaseList
    from tests.conftest import STUB_TEST_CASES

    _final_brd(stub_ba_agent, sow_file, sample_metadata)

    class _FakeStructuredLLM:
        def __init__(self):
            self.calls = []

        def invoke(self, prompt):
            self.calls.append(prompt)
            return TestCaseList(**STUB_TEST_CASES)

    agent = TestCaseAgent(structured=True)          # constructs, no network at init
    fake = _FakeStructuredLLM()
    monkeypatch.setattr(agent, "_structured_llm", fake)

    qa = TestCaseService(project_id=PID, agent=agent)
    v1 = qa.generate()

    assert fake.calls, "the structured LLM should have been invoked"
    assert v1.version == 1 and v1.source == "initial"
    assert "## TC-001 — " in v1.content and "## TC-002 — " in v1.content
    assert "**Version:** 1" in v1.content
    assert v1.source_ref == "brd_v1;hld_vnone;lld_vnone;us_vnone"


def test_fallback_structured_false_uses_freeform_invoke(monkeypatch):
    """structured=False must leave the original _invoke() string path in place."""
    from app.agents.test_case.agent import TestCaseAgent

    agent = TestCaseAgent(structured=False)
    assert agent._structured is False
    assert agent._structured_llm is None

    captured = {}
    monkeypatch.setattr(agent, "_invoke", lambda prompt: captured.setdefault("p", prompt) or '{"test_cases": []}')
    agent.generate_test_cases(
        brd_text="BRD body",
        hld_text="(no accepted HLD available)",
        lld_text="(no accepted LLD available)",
        user_stories_text="(no user stories available)",
        metadata=type("M", (), {
            "project_name": "P", "client_name": "C", "project_type": "T",
            "industry": "I", "language": "English",
        })(),
    )
    assert "BRD body" in captured["p"]
