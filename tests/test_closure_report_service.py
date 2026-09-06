"""
Phase 7 — Closure Report Service (deterministic tests, no Gemini).

Upstream streams are built with the REAL BA / SA / IUS / LLD / QA services wired
to the existing stub agents from `tests/conftest.py`; the Closure Report Service
runs against a `StubClosureReportAgent`. `app/quality/*` and every other agent's
logic is exercised as-is (never re-implemented or weakened here).
"""

import json

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.closure_report.service import (
    ClosureReportLockedError,
    ClosureReportService,
    InvalidClosureNarrativeError,
    NoFinalBRDError,
    STATUS_NOT_READY,
    STATUS_OPEN_ITEMS,
    STATUS_READY,
)
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService
from app.services.version_service import BRDVersion
from tests.conftest import (
    STUB_CLOSURE_NARRATIVE,
    StubBAAgent,
    StubClosureReportAgent,
    StubLLDAgent,
    StubSAAgent,
    StubTestCaseAgent,
    StubUserStoryAgent,
)

PID = "clr7"


# --- pipeline construction helpers ---------------------------------------

def _brd(pid, sow_file, sample_metadata, *, final=True):
    ba = BusinessAnalystService(project_id=pid, agent=StubBAAgent())
    ba.generate_initial_brd(sow_file, sample_metadata)
    if final:
        ba.choose_final_brd(1)
    return ba


def _pipeline(
    pid, sow_file, sample_metadata, *,
    brd_final=True, with_hld=True, hld_final=True,
    with_us=True, with_lld=True, lld_final=True,
    with_tc=True, tc_final=True,
):
    """Build a project to a configurable degree of completeness."""
    ba = _brd(pid, sow_file, sample_metadata, final=brd_final)
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=StubSAAgent())
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=StubUserStoryAgent())
    lld = LowLevelDesignService(project_id=pid, sa_service=sa, ba_service=ba, agent=StubLLDAgent())
    tc = TestCaseService(project_id=pid, agent=StubTestCaseAgent())

    if with_hld:
        sa.generate_initial_hld()
        if hld_final:
            sa.choose_final_hld(1)
    if with_us:
        us.generate_initial_stories()
    if with_lld and with_hld and hld_final:
        lld.generate_initial_lld()
        if lld_final:
            lld.choose_final_lld(1)
    if with_tc and brd_final:
        tc.generate()
        if tc_final:
            tc.choose_final(1)
    return ba, sa, us, lld, tc


def _svc(pid=PID, agent=None):
    return ClosureReportService(project_id=pid, agent=agent or StubClosureReportAgent())


def _status_line(content: str) -> str:
    return next(l for l in content.splitlines() if l.startswith("**Closure Status:**"))


# =====================================================================
# 1. empty project / missing BRD / missing final BRD
# =====================================================================

def test_empty_project_generate_raises_no_final_brd():
    with pytest.raises(NoFinalBRDError):
        _svc("empty7").generate()


def test_brd_exists_but_not_final_raises(sow_file, sample_metadata):
    _brd("clr_draftbrd", sow_file, sample_metadata, final=False)
    with pytest.raises(NoFinalBRDError):
        _svc("clr_draftbrd").generate()
    assert _svc("clr_draftbrd").get_all_versions() == []


def test_final_brd_only_generates_not_ready(sow_file, sample_metadata):
    _brd("clr_brdonly", sow_file, sample_metadata)
    cr = _svc("clr_brdonly")
    v1 = cr.generate()
    assert isinstance(v1, BRDVersion) and v1.version == 1 and v1.source == "initial"
    assert _status_line(v1.content) == f"**Closure Status:** {STATUS_NOT_READY}"
    assert v1.source_ref == "brd_v1;hld_vnone;lld_vnone;us_vnone;tc_vnone"


# =====================================================================
# 2. missing / non-final HLD, LLD; missing user stories / test cases
# =====================================================================

@pytest.mark.parametrize("kw,expect_blocker", [
    (dict(with_hld=False), "High-Level Design"),
    (dict(hld_final=False), "High-Level Design"),
    (dict(with_lld=False), "Low-Level Design"),
    (dict(lld_final=False), "Low-Level Design"),
    (dict(with_us=False), "user stories"),
    (dict(with_tc=False), "Test Cases"),
    (dict(tc_final=False), "Test Cases"),
])
def test_incomplete_evidence_is_reported_not_fatal(sow_file, sample_metadata, kw, expect_blocker):
    pid = "clr_" + "_".join(f"{k}{v}" for k, v in kw.items())
    _pipeline(pid, sow_file, sample_metadata, **kw)
    v = _svc(pid).generate()
    assert _status_line(v.content) == f"**Closure Status:** {STATUS_NOT_READY}"
    assert "## 8. Risks, Gaps and Outstanding Items" in v.content
    assert expect_blocker.lower() in v.content.lower()


def test_tc_exists_but_not_final_report_does_not_claim_finalized(sow_file, sample_metadata):
    _pipeline("clr_tcdraft", sow_file, sample_metadata, tc_final=False)
    v = _svc("clr_tcdraft").generate()
    # Section 3 row for Test Cases: exists yes, latest v1, final None
    assert "| Test Cases | Yes | v1 | None |" in v.content
    assert "draft only (not finalized)" in v.content
    assert _status_line(v.content) == f"**Closure Status:** {STATUS_NOT_READY}"


def test_draft_vs_final_hld_is_explicit(sow_file, sample_metadata):
    # HLD final v1, then a newer draft v2 exists and must NOT be treated as accepted.
    ba, sa, us, lld, tc = _pipeline("clr_hld2", sow_file, sample_metadata)
    sa.unlock_final_hld()
    sa.save_manual_edit(sa.get_version(1).content + "\n\nEdit.\n")  # HLD v2 draft
    v = _svc("clr_hld2").generate()
    assert "a newer draft v2 exists and was NOT used" in v.content
    assert v.source_ref.split(";")[1] == "hld_v1"  # accepted final, not the draft


def test_version_selection_note_flags_draft_beyond_final():
    info = {"exists": True, "latest_version": 2, "final_version": 1, "final_stage": True}
    note = ClosureReportService._version_selection_note("hld", info)
    assert "accepted final v1" in note and "newer draft v2 exists and was NOT used" in note

    not_final = {"exists": True, "latest_version": 3, "final_version": None, "final_stage": True}
    assert "NOT finalized" in ClosureReportService._version_selection_note("lld", not_final)

    us_info = {"exists": True, "latest_version": 4, "final_version": None, "final_stage": False}
    assert "no finalization stage" in ClosureReportService._version_selection_note("user_stories", us_info)


# =====================================================================
# 3. fully complete project -> READY / OPEN_ITEMS
# =====================================================================

def test_fully_complete_project_is_ready(sow_file, sample_metadata):
    _pipeline("clr_full", sow_file, sample_metadata)
    v = _svc("clr_full").generate()
    assert _status_line(v.content) == f"**Closure Status:** {STATUS_READY}"
    assert "**Closure Readiness:** Ready for closure" in v.content
    for section in (
        "## 1. Project Closure Summary", "## 2. Scope Summary",
        "## 3. SDLC Artifact Summary", "## 4. Requirements & Traceability",
        "## 5. User Story Summary", "## 6. Test Coverage Summary",
        "## 7. Quality Findings", "## 8. Risks, Gaps and Outstanding Items",
        "## 9. Final Closure Assessment",
    ):
        assert section in v.content


# =====================================================================
# 4. deterministic status rules (pure function, every branch)
# =====================================================================

def _art(exists, final, latest=None, *, stage=True):
    return {
        "exists": exists, "final_version": final, "final_stage": stage,
        "latest_version": latest if latest is not None else (final or (1 if exists else None)),
    }


def _evidence(*, brd=1, hld=1, lld=1, us_exists=True, tc_exists=True, tc_final=1,
              uncov_req=(), uncov_us=(), grounding=0, orphan=0, hld_latest=None):
    return {
        "artifact_status": {
            "brd": _art(True, brd),
            "hld": _art(hld is not None, hld, hld_latest),
            "lld": _art(lld is not None, lld),
            "user_stories": _art(us_exists, None, 1 if us_exists else None, stage=False),
            "test_cases": _art(tc_exists, tc_final, 1 if tc_exists else None),
        },
        "quality_findings": {
            "uncovered_requirements": list(uncov_req),
            "uncovered_user_stories": list(uncov_us),
            "grounding_findings": {"total": grounding},
            "orphan_references": {"total": orphan},
        },
    }


def test_status_ready_when_all_final_and_clean():
    assert ClosureReportService._decide_closure_status(_evidence()) == STATUS_READY


@pytest.mark.parametrize("kw", [
    dict(uncov_req=["FR-2"]),
    dict(uncov_us=["US-002"]),
    dict(grounding=1),
    dict(orphan=1),
])
def test_status_open_items_for_each_non_blocking_finding(kw):
    assert ClosureReportService._decide_closure_status(_evidence(**kw)) == STATUS_OPEN_ITEMS


@pytest.mark.parametrize("kw", [
    dict(hld=None),
    dict(lld=None),
    dict(us_exists=False),
    dict(tc_exists=False),
    dict(tc_final=None),
])
def test_status_not_ready_when_required_final_evidence_missing(kw):
    assert ClosureReportService._decide_closure_status(_evidence(**kw)) == STATUS_NOT_READY


def test_status_missing_required_dominates_open_items():
    ev = _evidence(hld=None, uncov_req=["FR-2"], orphan=3)
    assert ClosureReportService._decide_closure_status(ev) == STATUS_NOT_READY


# =====================================================================
# 5. versioning / regeneration / no auto-finalization
# =====================================================================

def test_regenerate_creates_a_new_version_and_preserves_the_prior(sow_file, sample_metadata):
    _pipeline("clr_regen", sow_file, sample_metadata)
    cr = _svc("clr_regen")
    v1 = cr.generate()
    v1_snapshot = cr.get_version(1).content
    v2 = cr.regenerate()
    assert v2.version == 2 and v2.source == "ai_refine"
    assert [x.version for x in cr.get_all_versions()] == [1, 2]
    assert cr.get_version(1).content == v1_snapshot  # untouched


def test_multiple_generations_accumulate(sow_file, sample_metadata):
    _pipeline("clr_multi", sow_file, sample_metadata)
    cr = _svc("clr_multi")
    cr.generate(); cr.regenerate(); cr.regenerate()
    assert [x.version for x in cr.get_all_versions()] == [1, 2, 3]
    assert cr.get_version(1).source == "initial"
    assert {x.source for x in cr.get_all_versions()[1:]} == {"ai_refine"}


def test_generate_never_finalizes(sow_file, sample_metadata):
    _pipeline("clr_nofinal", sow_file, sample_metadata)
    cr = _svc("clr_nofinal")
    cr.generate()
    cr.regenerate()
    assert cr.get_final() is None
    assert cr.is_locked() is False
    assert all(not v.is_final and not v.is_locked for v in cr.get_all_versions())


def test_choose_final_then_locked_blocks_regeneration(sow_file, sample_metadata):
    _pipeline("clr_lock", sow_file, sample_metadata)
    cr = _svc("clr_lock")
    cr.generate()
    cr.choose_final(1)
    assert cr.is_locked() is True
    with pytest.raises(ClosureReportLockedError):
        cr.regenerate()
    with pytest.raises(ClosureReportLockedError):
        cr.generate()


def test_unlock_final_allows_regeneration_again(sow_file, sample_metadata):
    _pipeline("clr_unlock", sow_file, sample_metadata)
    cr = _svc("clr_unlock")
    cr.generate()
    cr.choose_final(1)
    cr.unlock_final()
    v2 = cr.regenerate()
    assert v2.version == 2
    assert cr.get_version(1).is_final is True  # is_final retained, lock released


# =====================================================================
# 6. provenance / evidence reuse / narrative boundary
# =====================================================================

def test_source_ref_and_built_from_record_exact_versions(sow_file, sample_metadata):
    _pipeline("clr_prov", sow_file, sample_metadata)
    v = _svc("clr_prov").generate()
    assert v.source_ref == "brd_v1;hld_v1;lld_v1;us_v1;tc_v1"
    assert "Built From:" in v.content
    assert "BRD v1, HLD v1, LLD v1, User Stories v1, Test Cases v1" in v.content


def test_optional_absent_artifact_recorded_as_none_not_int(sow_file, sample_metadata):
    _pipeline("clr_absent", sow_file, sample_metadata, with_hld=False, with_lld=False)
    v = _svc("clr_absent").generate()
    toks = dict(t.split("_v") for t in v.source_ref.split(";"))
    assert toks["hld"] == "none" and toks["lld"] == "none"
    assert toks["brd"] == "1"


def test_agent_receives_evidence_and_status_only_never_computes(sow_file, sample_metadata):
    _pipeline("clr_agentio", sow_file, sample_metadata)
    stub = StubClosureReportAgent()
    ClosureReportService(project_id="clr_agentio", agent=stub).generate()
    assert len(stub.calls) == 1
    evidence_json, closure_status, _md = stub.calls[0]
    assert closure_status in (STATUS_READY, STATUS_OPEN_ITEMS, STATUS_NOT_READY)
    payload = json.loads(evidence_json)
    # every fact the model might need is already computed and supplied
    assert "requirements_traceability" in payload
    assert "closure_status" in payload and payload["closure_status"] == closure_status
    assert payload["evidence_summary"]["requirement_coverage_pct"] is not None


def test_report_embeds_the_agent_narrative(sow_file, sample_metadata):
    _pipeline("clr_narr", sow_file, sample_metadata)
    v = _svc("clr_narr").generate()
    assert STUB_CLOSURE_NARRATIVE["executive_summary"] in v.content
    assert STUB_CLOSURE_NARRATIVE["findings_summary"] in v.content
    assert STUB_CLOSURE_NARRATIVE["closure_summary"] in v.content
    # The model's `limitations` field is still generated + validated, but it is
    # intentionally NOT re-rendered in Section 9 — the deterministic `_LIMITATIONS`
    # bullets and the closing disclaimer already state it once (de-duplication).
    assert v.content.count(STUB_CLOSURE_NARRATIVE["limitations"]) == 0


def test_quality_findings_section_reuses_quality_report_numbers(sow_file, sample_metadata):
    from app.quality.project_quality_report import build_project_quality_report_for_project
    _pipeline("clr_reuse", sow_file, sample_metadata)
    qr = build_project_quality_report_for_project("clr_reuse")
    v = _svc("clr_reuse").generate()
    assert (f"**Ungrounded test-case references:** "
            f"{qr['grounding_findings']['total']}") in v.content
    assert f"**Orphan references:** {qr['orphan_references']['total']}" in v.content
    assert (f"**Total requirements:** "
            f"{qr['requirement_coverage']['total']}") in v.content


def test_section6_direct_and_story_only_partition_the_covered_set(sow_file, sample_metadata):
    """The Section 6 test-coverage decomposition must be mutually exclusive and
    sum to Section 4's covered-requirement count.

    The stub pipeline's single requirement FR-1 is reached BOTH directly
    (TC brd_reference=FR-1) AND via US-001 — the exact overlap case that made
    the previous `direct + story_only` counts exceed the covered total.
    """
    _pipeline("clr_sec6", sow_file, sample_metadata)
    cr = _svc("clr_sec6")
    evidence = cr._assemble_evidence()
    tcs = evidence["test_coverage_summary"]
    covered = evidence["requirements_traceability"]["covered_requirements"]

    direct = tcs["covered_requirements_with_direct_test_case"]
    story_only = tcs["covered_requirements_via_user_story_only"]
    assert direct + story_only == covered
    assert story_only >= 0
    # the old overlapping keys are gone
    assert "direct_requirement_test_coverage" not in tcs
    assert "story_mediated_test_coverage" not in tcs

    v = cr.generate()
    assert ("**Covered requirements with a directly-cited test case:** "
            f"{direct}") in v.content
    assert ("**Covered requirements whose only test-case evidence is via a "
            f"user story:** {story_only}") in v.content
    assert (f"sum to the {covered} covered requirements in Section 4" in v.content)


def test_section8_9_distinguish_blocking_from_non_blocking(sow_file, sample_metadata):
    # Fully complete stub project -> READY, no blockers, no outstanding items.
    _pipeline("clr_sec8", sow_file, sample_metadata)
    v = _svc("clr_sec8").generate()
    assert "Closure status **READY_FOR_CLOSURE**" in v.content
    assert "**Blocking issues**" in v.content
    assert "**Outstanding non-blocking items**" in v.content
    assert "None — no issue prevents closure." in v.content
    # no misleading runtime-testing language from the deterministic layer
    assert "end-to-end coverage" not in v.content
    assert "user story/stories" not in v.content


def test_outstanding_items_wording_is_coverage_not_verification():
    ev = {"quality_findings": {
        "uncovered_requirements": ["FR-2", "NFR-1"],
        "uncovered_user_stories": ["US-002", "US-003"],
        "grounding_findings": {"total": 0},
        "orphan_references": {"total": 0},
    }}
    items = ClosureReportService._outstanding_items(ev)
    joined = " ".join(items)
    assert "2 requirements have no test-case coverage by the available traceability evidence" in joined
    assert "2 user stories have no linked test cases" in joined
    assert "unverified" not in joined.lower()
    assert "end-to-end" not in joined.lower()
    assert "user story/stories" not in joined

    one = ClosureReportService._outstanding_items({"quality_findings": {
        "uncovered_requirements": ["FR-2"], "uncovered_user_stories": ["US-002"],
        "grounding_findings": {"total": 0}, "orphan_references": {"total": 0},
    }})
    assert "1 requirement has no test-case coverage" in one[0]
    assert "1 user story has no linked test cases" in one[1]


# =====================================================================
# 7. malformed / retry / JSON serializability / purity
# =====================================================================

def test_malformed_agent_output_raises_invalid_narrative(sow_file, sample_metadata):
    class _BadJSON:
        def synthesize_narrative(self, *a, **k):
            return "not json at all"

    _pipeline("clr_badjson", sow_file, sample_metadata)
    cr = ClosureReportService(project_id="clr_badjson", agent=_BadJSON())
    with pytest.raises(InvalidClosureNarrativeError):
        cr.generate()
    assert cr.get_all_versions() == []


def test_missing_narrative_field_raises(sow_file, sample_metadata):
    class _Partial:
        def synthesize_narrative(self, *a, **k):
            return json.dumps({"executive_summary": "x"})  # missing the rest

    _pipeline("clr_partial", sow_file, sample_metadata)
    cr = ClosureReportService(project_id="clr_partial", agent=_Partial())
    with pytest.raises(InvalidClosureNarrativeError):
        cr.generate()


def test_agent_error_propagates_and_persists_nothing(sow_file, sample_metadata):
    from app.agents.closure_report.agent import ClosureReportAgentError

    class _Boom:
        def synthesize_narrative(self, *a, **k):
            raise ClosureReportAgentError("Gemini exploded")

    _pipeline("clr_boom", sow_file, sample_metadata)
    cr = ClosureReportService(project_id="clr_boom", agent=_Boom())
    with pytest.raises(ClosureReportAgentError):
        cr.generate()
    assert cr.get_all_versions() == []


def test_retry_helpers_match_the_repo_convention():
    from app.agents.closure_report import agent as cr_agent
    from pydantic import ValidationError

    class _Boom(Exception):
        pass

    assert cr_agent._is_transient_llm_error(_Boom("503 UNAVAILABLE")) is True
    assert cr_agent._is_transient_llm_error(_Boom("model overloaded, try again")) is True
    assert cr_agent._is_transient_llm_error(_Boom("bad request: invalid arg")) is False
    try:
        cr_agent.ClosureNarrative(executive_summary="")
    except ValidationError as ve:
        assert cr_agent._is_transient_llm_error(ve) is False
    for attempt in (1, 2, 3):
        lo = min(cr_agent._RETRY_MAX_DELAY_S,
                 cr_agent._RETRY_BASE_DELAY_S * 2 ** (attempt - 1)) / 2
        assert lo <= cr_agent._retry_backoff_seconds(attempt) <= lo * 2


def test_transient_classification_covers_transport_drops_phase11a():
    """Phase 11A: the mid-response server disconnect that aborted a full
    pipeline run in the Phase 11 benchmark must now classify as transient."""
    from app.agents.closure_report import agent as cr_agent

    class _Boom(Exception):
        pass

    for text in (
        "Server disconnected without sending a response.",
        "httpx.RemoteProtocolError: Server disconnected",
        "Connection reset by peer",
        "Connection aborted.",
        "IncompleteRead(0 bytes read)",
        "peer closed connection without sending complete message body "
        "(incomplete read)",
    ):
        assert cr_agent._is_transient_llm_error(_Boom(text)) is True, text

    class RemoteProtocolError(Exception):
        pass

    assert cr_agent._is_transient_llm_error(RemoteProtocolError()) is True
    # genuinely non-transient errors are still not retried
    assert cr_agent._is_transient_llm_error(_Boom("401 UNAUTHENTICATED")) is False


def test_closure_structured_retry_is_bounded_phase11a(monkeypatch):
    """A persistently transient failure stops after `_RETRY_MAX_ATTEMPTS`
    application-level attempts and raises — it never loops indefinitely."""
    from app.agents.closure_report.agent import (
        ClosureReportAgent,
        ClosureReportAgentError,
        _RETRY_MAX_ATTEMPTS,
    )

    monkeypatch.setattr("app.agents.closure_report.agent.time.sleep", lambda s: None)

    class _AlwaysDisconnect:
        def __init__(self):
            self.calls = 0

        def invoke(self, prompt):
            self.calls += 1
            raise RuntimeError("Server disconnected without sending a response.")

    agent = ClosureReportAgent(structured=True)
    fake = _AlwaysDisconnect()
    agent._structured_llm = fake  # inject before first use (lazy path honours it)

    with pytest.raises(ClosureReportAgentError):
        agent._invoke_structured("prompt")

    assert fake.calls == _RETRY_MAX_ATTEMPTS  # bounded, not infinite


def test_report_content_and_evidence_are_json_safe(sow_file, sample_metadata):
    _pipeline("clr_jsonsafe", sow_file, sample_metadata)
    v = _svc("clr_jsonsafe").generate()
    # the stored record round-trips through JSON (VersionService persists JSON)
    round_tripped = BRDVersion(**json.loads(json.dumps(v.model_dump())))
    assert round_tripped.content == v.content
    assert round_tripped.source_ref == v.source_ref


def test_pure_evidence_helpers_do_not_persist(sow_file, sample_metadata, isolated_output_dir):
    _pipeline("clr_pure", sow_file, sample_metadata)
    cr = _svc("clr_pure")
    proj = isolated_output_dir / "clr_pure"
    before = {p: p.read_bytes() for p in proj.rglob("versions.json")}

    evidence = cr._assemble_evidence()
    status = ClosureReportService._decide_closure_status(evidence)
    evidence["blockers"] = ClosureReportService._blockers(evidence, status)
    evidence["outstanding_items"] = ClosureReportService._outstanding_items(evidence)
    ClosureReportService._evidence_summary(evidence)
    json.dumps(evidence)  # serializable

    assert {p: p.read_bytes() for p in proj.rglob("versions.json")} == before
    assert (proj / "closure_report" / "versions.json").exists() is False


def test_deterministic_status_is_stable_across_repeated_assembly(sow_file, sample_metadata):
    _pipeline("clr_stable", sow_file, sample_metadata)
    cr = _svc("clr_stable")
    statuses = {
        ClosureReportService._decide_closure_status(cr._assemble_evidence())
        for _ in range(3)
    }
    assert statuses == {STATUS_READY}


# =====================================================================
# Phase 10B — Closure Report staleness (non-blocking; no auto-regen)
# =====================================================================

def test_closure_staleness_none_before_generation_and_on_a_fresh_report(sow_file, sample_metadata):
    _pipeline("clr_stale_fresh", sow_file, sample_metadata)
    cr = _svc("clr_stale_fresh")

    assert cr.recorded_source_versions() is None       # no closure version yet
    assert cr.stale_sources() == []
    assert cr.is_stale() is False

    v1 = cr.generate()
    assert cr.recorded_source_versions() == {"brd": 1, "hld": 1, "lld": 1, "us": 1, "tc": 1}
    assert cr.current_source_versions() == {"brd": 1, "hld": 1, "lld": 1, "us": 1, "tc": 1}
    assert cr.stale_sources() == []                    # SCENARIO 1: no false stale
    assert cr.is_stale() is False


def test_closure_is_marked_stale_after_brd_changes_but_never_regenerates(sow_file, sample_metadata):
    """SCENARIO 2: BRD v1 -> HLD/LLD/TC/Closure v1; then BRD v2 final.
    Closure v1 stays byte-identical, is reported stale, names BRD, and nothing
    is regenerated, finalized, or unlocked."""
    ba, sa, us, lld, tc = _pipeline("clr_stale_brd", sow_file, sample_metadata)
    cr = _svc("clr_stale_brd")
    v1 = cr.generate()
    v1_dump = cr.get_version(1).model_dump()

    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\n\nNew requirement.\n")
    ba.choose_final_brd(2)

    assert cr.stale_sources() == ["BRD"]
    assert cr.is_stale() is True
    assert cr.recorded_source_versions()["brd"] == 1
    assert cr.current_source_versions()["brd"] == 2
    # closure report untouched — still 1 version, byte-identical, still not final
    assert [v.version for v in cr.get_all_versions()] == [1]
    assert cr.get_version(1).model_dump() == v1_dump
    assert cr.get_final() is None

    # an explicit regenerate clears staleness (new version; old one kept)
    v2 = cr.regenerate()
    assert v2.version == 2
    assert cr.stale_sources() == []
    assert [v.version for v in cr.get_all_versions()] == [1, 2]


def test_closure_staleness_covers_all_five_sources_including_none_to_v_transition(
    sow_file, sample_metadata
):
    # Generate the closure report from a project with NO HLD/LLD/TC.
    ba, sa, us, lld, tc = _pipeline(
        "clr_stale_all", sow_file, sample_metadata,
        with_hld=False, with_us=True, with_lld=False, with_tc=False,
    )
    cr = _svc("clr_stale_all")
    cr.generate()
    assert cr.recorded_source_versions() == {"brd": 1, "hld": None, "lld": None,
                                             "us": 1, "tc": None}
    assert cr.stale_sources() == []

    # user stories refined -> latest changes -> "User Stories" stale
    us.save_manual_edit(us.get_version(1).content + "\nextra\n")
    assert cr.stale_sources() == ["User Stories"]

    # a final HLD now appears (None -> v1) -> "HLD" also stale (materially changes
    # the closure report's artifact summary + status, unlike Test Cases context)
    sa.generate_initial_hld()
    sa.choose_final_hld(1)
    assert set(cr.stale_sources()) == {"HLD", "User Stories"}


def test_closure_stale_sources_uses_latest_user_stories_not_a_finalized_older_one(
    sow_file, sample_metadata
):
    ba, sa, us, lld, tc = _pipeline("clr_stale_us", sow_file, sample_metadata)
    cr = _svc("clr_stale_us")
    cr.generate()                                   # recorded us == 1
    us.save_manual_edit(us.get_version(1).content + "\nv2\n")   # latest us == 2
    us.choose_final_stories(1)                      # is_final flag on the OLDER v1

    assert cr.current_source_versions()["us"] == 2  # latest, not the finalized v1
    assert cr.stale_sources() == ["User Stories"]
