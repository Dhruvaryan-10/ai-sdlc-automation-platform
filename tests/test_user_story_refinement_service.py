"""
User Story Refinement service tests — the Phase 5 acceptance checklist.

Deterministic; the refinement LLM is a stub injected via `agent=`. Upstream
artifact streams (BRD / HLD / LLD / initial user stories) are populated with the
real Phase 1–4 services + their stub agents, purely as test setup — the
refinement package itself imports none of them.
"""

import ast
import re
from pathlib import Path

import pytest
from docx import Document

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.user_story_refinement.service import (
    NoFinalBRDError,
    NoInitialUserStoriesError,
    RefinementLockedError,
    UserStoryRefinementService,
)
from app.document_generator.brd_generator import generate_user_stories_docx

PID = "proj"

# Kept in sync with tests/conftest.py::STUB_REFINEMENT_MARKER.
STUB_REFINEMENT_MARKER = "**Refinement note:** reconciled against project artifacts."


# --- setup helpers --------------------------------------------------------

def _final_brd(stub_ba_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    return ba


def _initial_stories(ba, stub_us_agent):
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()
    return us


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


def _usr(stub_usr_agent):
    return UserStoryRefinementService(project_id=PID, agent=stub_usr_agent)


# --- TEST 1: no final BRD -> blocked ------------------------------------

def test_no_final_brd_blocks_refinement(stub_ba_agent, stub_usr_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)  # generated, NOT finalised
    # (Phase 3 stories can't exist without a final BRD, so this is the only
    # reachable "no final BRD" state — the BRD gate is checked first regardless.)
    with pytest.raises(NoFinalBRDError):
        _usr(stub_usr_agent).refine()


# --- TEST 2: no existing user stories -> blocked ---------------------

def test_no_initial_user_stories_blocks_refinement(stub_ba_agent, stub_usr_agent,
                                                   sow_file, sample_metadata):
    _final_brd(stub_ba_agent, sow_file, sample_metadata)  # BRD final, but no stories
    with pytest.raises(NoInitialUserStoriesError):
        _usr(stub_usr_agent).refine()


# --- TEST 3: BRD + stories only -> refine succeeds -----------------

def test_refine_with_brd_and_stories_only(stub_ba_agent, stub_us_agent, stub_usr_agent,
                                          sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _initial_stories(ba, stub_us_agent)
    usr = _usr(stub_usr_agent)

    v2 = usr.refine()

    assert v2.version == 2
    assert v2.source == "ai_refine"
    assert v2.source_ref == "brd_v1;us_v1;hld_vnone;lld_vnone"
    assert "**Version:** 2" in v2.content
    assert "**Version:** 0" not in v2.content
    # agent received the real BRD and the sentinels for HLD/LLD
    current_stories, brd_text, hld_text, lld_text, _, _ = stub_usr_agent.refine_calls[0]
    assert "Business Requirement Document" in brd_text
    assert hld_text == "(no accepted HLD available)"
    assert lld_text == "(no accepted LLD available)"


# --- TEST 4 + 5: HLD / LLD supplied as context --------------------

def test_hld_and_lld_supplied_as_context(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                         stub_lld_agent, stub_usr_agent, sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _initial_stories(ba, stub_us_agent)
    sa = _final_hld(ba, stub_sa_agent)
    _final_lld(sa, ba, stub_lld_agent)

    v2 = _usr(stub_usr_agent).refine()

    assert v2.source_ref == "brd_v1;us_v1;hld_v1;lld_v1"
    _, _, hld_text, lld_text, _, _ = stub_usr_agent.refine_calls[0]
    assert "High-Level Design" in hld_text
    assert "Low-Level Design" in lld_text
    assert "HLD v1" in v2.note and "LLD v1" in v2.note


# --- TEST 6: HLD stream exists but no ACCEPTED HLD -> sentinel, still refines ---

def test_unaccepted_hld_uses_sentinel(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                      stub_usr_agent, sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _initial_stories(ba, stub_us_agent)
    # an HLD is generated but never finalised -> not "accepted" -> treated as absent
    SolutionArchitectService(project_id=PID, ba_service=ba,
                             agent=stub_sa_agent).generate_initial_hld()

    v2 = _usr(stub_usr_agent).refine()
    assert v2.source_ref == "brd_v1;us_v1;hld_vnone;lld_vnone"
    _, _, hld_text, lld_text, _, _ = stub_usr_agent.refine_calls[0]
    assert hld_text == "(no accepted HLD available)"
    assert lld_text == "(no accepted LLD available)"


# --- TEST 7: final HLD but draft/missing LLD -> LLD sentinel -----

def test_final_hld_but_no_final_lld(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                    stub_lld_agent, stub_usr_agent, sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _initial_stories(ba, stub_us_agent)
    sa = _final_hld(ba, stub_sa_agent)
    # generate an LLD but DO NOT finalise it
    LowLevelDesignService(project_id=PID, sa_service=sa, ba_service=ba,
                          agent=stub_lld_agent).generate_initial_lld()

    v2 = _usr(stub_usr_agent).refine()
    assert v2.source_ref == "brd_v1;us_v1;hld_v1;lld_vnone"
    _, _, hld_text, lld_text, _, _ = stub_usr_agent.refine_calls[0]
    assert "High-Level Design" in hld_text
    assert lld_text == "(no accepted LLD available)"


# --- TEST 8 + 9: US-NNN ids stable, unaffected stories unchanged --

def _strip_managed_lines(text: str) -> str:
    """Normalise the application-managed header lines (Version / Source / Built From).

    The service stamps '**Version:**', and (Phase 5 provenance fix) rewrites
    '**Source:**' and inserts '**Built From:**' on an artifact-refined version.
    Everything else — every story body — must be byte-identical.
    """
    text = re.sub(r"\*\*Version:\*\*\s*\d+", "**Version:** N", text)
    text = re.sub(r"^\*\*Source:\*\*[^\n]*$", "**Source:** X", text, flags=re.MULTILINE)
    text = re.sub(r"^\*\*Built From:\*\*[^\n]*\n?", "", text, flags=re.MULTILINE)
    return text


def test_ids_stable_and_unaffected_stories_unchanged(stub_ba_agent, stub_us_agent,
                                                     stub_usr_agent, sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    us = _initial_stories(ba, stub_us_agent)
    v1_content = us.get_version(1).content

    v2 = _usr(stub_usr_agent).refine()

    assert "## US-001 — Customer Registration" in v2.content  # id + title preserved
    # every story body up to the appended marker is byte-identical to v1
    # (only the application-managed header lines legitimately differ).
    body_before_marker = v2.content.split(STUB_REFINEMENT_MARKER)[0].rstrip()
    assert _strip_managed_lines(body_before_marker) == _strip_managed_lines(v1_content.rstrip())


# --- TEST 10 + 11: new version; previous versions unchanged ------

def test_refinement_creates_new_version_and_preserves_history(stub_ba_agent, stub_us_agent,
                                                              stub_usr_agent, sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    us = _initial_stories(ba, stub_us_agent)
    v1_dump = us.get_version(1).model_dump()

    usr = _usr(stub_usr_agent)
    v2 = usr.refine()
    v3 = usr.refine()

    all_versions = InitialUserStoryService(project_id=PID, ba_service=ba).get_all_versions()
    assert [v.version for v in all_versions] == [1, 2, 3]
    assert all_versions[0].model_dump() == v1_dump  # v1 entry untouched
    assert v2.version == 2 and v3.version == 3


# --- TEST 12 + 13: locked rejects refinement; unlock allows it --

def test_locked_final_stories_reject_refinement(stub_ba_agent, stub_us_agent,
                                                stub_usr_agent, sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    us = _initial_stories(ba, stub_us_agent)
    us.choose_final_stories(1)  # marks final + locks

    with pytest.raises(RefinementLockedError):
        _usr(stub_usr_agent).refine()

    us.unlock_final_stories()
    v2 = _usr(stub_usr_agent).refine()
    assert v2.version == 2


# --- TEST 14/15/16/17: source changes mark stale, never mutate --

def _refined_project(stub_ba_agent, stub_us_agent, stub_sa_agent, stub_lld_agent,
                     stub_usr_agent, sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _initial_stories(ba, stub_us_agent)
    sa = _final_hld(ba, stub_sa_agent)
    _final_lld(sa, ba, stub_lld_agent)
    usr = _usr(stub_usr_agent)
    usr.refine()  # v2, source_ref = brd_v1;us_v1;hld_v1;lld_v1
    return ba, sa, usr


def test_brd_change_marks_stale_without_mutation(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                                 stub_lld_agent, stub_usr_agent, sow_file, sample_metadata):
    ba, sa, usr = _refined_project(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                   stub_lld_agent, stub_usr_agent, sow_file, sample_metadata)
    snapshot = [v.model_dump() for v in usr._stories.get_all_versions()]
    assert usr.is_stale() is False

    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\n\nNew requirement.\n")
    ba.choose_final_brd(2)

    assert usr.is_stale() is True
    assert usr.stale_sources() == ["BRD"]
    assert [v.model_dump() for v in usr._stories.get_all_versions()] == snapshot


def test_hld_change_marks_stale_without_mutation(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                                 stub_lld_agent, stub_usr_agent, sow_file, sample_metadata):
    ba, sa, usr = _refined_project(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                   stub_lld_agent, stub_usr_agent, sow_file, sample_metadata)
    snapshot = [v.model_dump() for v in usr._stories.get_all_versions()]

    sa.unlock_final_hld()
    sa.save_manual_edit(sa.get_version(1).content + "\n\nNew component.\n")
    sa.choose_final_hld(2)

    assert usr.stale_sources() == ["HLD"]
    assert [v.model_dump() for v in usr._stories.get_all_versions()] == snapshot


def test_lld_change_marks_stale_without_mutation(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                                 stub_lld_agent, stub_usr_agent, sow_file, sample_metadata):
    ba, sa, usr = _refined_project(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                   stub_lld_agent, stub_usr_agent, sow_file, sample_metadata)
    snapshot = [v.model_dump() for v in usr._stories.get_all_versions()]

    lld = LowLevelDesignService(project_id=PID, sa_service=sa, ba_service=ba)
    lld.unlock_final_lld()
    lld.save_manual_edit(lld.get_version(1).content + "\n\nExtra table.\n")
    lld.choose_final_lld(2)

    assert usr.stale_sources() == ["LLD"]
    assert [v.model_dump() for v in usr._stories.get_all_versions()] == snapshot


def test_multiple_source_changes_one_stale_state_itemised(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                                          stub_lld_agent, stub_usr_agent, sow_file, sample_metadata):
    ba, sa, usr = _refined_project(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                   stub_lld_agent, stub_usr_agent, sow_file, sample_metadata)
    snapshot = [v.model_dump() for v in usr._stories.get_all_versions()]

    ba.unlock_final_brd(); ba.save_manual_edit(ba.get_version(1).content + "\nx\n"); ba.choose_final_brd(2)
    lld = LowLevelDesignService(project_id=PID, sa_service=sa, ba_service=ba)
    lld.unlock_final_lld(); lld.save_manual_edit(lld.get_version(1).content + "\ny\n"); lld.choose_final_lld(2)

    assert usr.is_stale() is True
    assert usr.stale_sources() == ["BRD", "LLD"]  # one stale state, itemised
    assert [v.model_dump() for v in usr._stories.get_all_versions()] == snapshot


# --- TEST 18 + 19: explicit re-refinement clears stale, new version --

def test_re_refinement_clears_stale_and_creates_new_version(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                                            stub_lld_agent, stub_usr_agent, sow_file, sample_metadata):
    ba, sa, usr = _refined_project(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                   stub_lld_agent, stub_usr_agent, sow_file, sample_metadata)
    ba.unlock_final_brd(); ba.save_manual_edit(ba.get_version(1).content + "\nx\n"); ba.choose_final_brd(2)
    assert usr.is_stale() is True

    v3 = usr.refine()

    assert v3.version == 3
    assert usr.is_stale() is False
    assert usr.recorded_source_versions() == {"brd": 2, "us": 2, "hld": 1, "lld": 1}


# --- TEST 23: refined stories still export to DOCX ------------------

def test_refined_stories_export_to_docx(stub_ba_agent, stub_us_agent, stub_usr_agent,
                                        sow_file, sample_metadata, tmp_path):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _initial_stories(ba, stub_us_agent)
    v2 = _usr(stub_usr_agent).refine()

    out = tmp_path / "UserStories_v2.docx"
    generate_user_stories_docx(v2.content, out)
    assert out.exists()
    doc = Document(str(out))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "US-001 — Customer Registration" in text


# --- TEST 24: no automatic re-refinement on source change ---------

def test_no_automatic_regeneration_on_source_change(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                                    stub_lld_agent, stub_usr_agent, sow_file, sample_metadata):
    ba, sa, usr = _refined_project(stub_ba_agent, stub_us_agent, stub_sa_agent,
                                   stub_lld_agent, stub_usr_agent, sow_file, sample_metadata)
    before = [v.model_dump() for v in usr._stories.get_all_versions()]
    calls_before = len(stub_usr_agent.refine_calls)

    ba.unlock_final_brd(); ba.save_manual_edit(ba.get_version(1).content + "\nx\n"); ba.choose_final_brd(2)
    _ = usr.is_stale()  # merely checking staleness must not trigger anything

    assert [v.model_dump() for v in usr._stories.get_all_versions()] == before
    assert len(stub_usr_agent.refine_calls) == calls_before


# --- metadata + independence-of-imports (item 21 lives here too) ---

def test_metadata_is_derived_from_stories(stub_ba_agent, stub_us_agent, stub_usr_agent,
                                          sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _initial_stories(ba, stub_us_agent)
    _usr(stub_usr_agent).refine()
    _, _, _, _, metadata, _ = stub_usr_agent.refine_calls[0]
    assert metadata.project_name == "Test Project"
    assert metadata.client_name == "Acme Corp"
    assert metadata.project_type == "Web Application"


def test_refinement_package_has_no_downstream_agent_imports():
    import app.agents.user_story_refinement.agent as usr_agent_mod
    import app.agents.user_story_refinement.service as usr_service_mod

    forbidden = ("initial_user_story", "solution_architect", "low_level_design")
    for mod in (usr_agent_mod, usr_service_mod):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert not any(f in name for name in imported for f in forbidden), imported


# --- Phase 5 provenance correction (Phase 6 items 28-30) ------------------

def test_initial_user_stories_document_label_is_accepted_brd(
    stub_ba_agent, stub_us_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    us = _initial_stories(ba, stub_us_agent)
    assert "**Source:** Accepted BRD" in us.get_version(1).content
    assert "**Built From:**" not in us.get_version(1).content


def test_artifact_refined_document_label_is_artifact_refinement(
    stub_ba_agent, stub_us_agent, stub_sa_agent, stub_lld_agent, stub_usr_agent,
    sow_file, sample_metadata,
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _initial_stories(ba, stub_us_agent)
    sa = _final_hld(ba, stub_sa_agent)
    _final_lld(sa, ba, stub_lld_agent)

    v2 = _usr(stub_usr_agent).refine()

    assert "**Source:** Artifact Refinement" in v2.content
    assert "**Source:** Accepted BRD" not in v2.content
    # the initial version is left untouched (append-only; historical versions unchanged)
    us = InitialUserStoryService(project_id=PID, ba_service=ba)
    assert "**Source:** Accepted BRD" in us.get_version(1).content


def test_artifact_refined_document_embeds_source_version_provenance(
    stub_ba_agent, stub_us_agent, stub_sa_agent, stub_lld_agent, stub_usr_agent,
    sow_file, sample_metadata,
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _initial_stories(ba, stub_us_agent)
    sa = _final_hld(ba, stub_sa_agent)
    _final_lld(sa, ba, stub_lld_agent)

    v2 = _usr(stub_usr_agent).refine()

    assert "**Built From:** BRD v1, HLD v1, LLD v1, User Stories v1" in v2.content


def test_artifact_refined_document_built_from_marks_missing_optional(
    stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    _initial_stories(ba, stub_us_agent)  # no HLD / LLD

    v2 = _usr(stub_usr_agent).refine()

    assert "**Source:** Artifact Refinement" in v2.content
    assert "**Built From:** BRD v1, HLD not used, LLD not used, User Stories v1" in v2.content
