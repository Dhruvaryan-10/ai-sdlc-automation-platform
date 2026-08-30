"""
Low-Level Design service tests — the Phase 4 acceptance checklist.

All deterministic; the LLM is a stub injected via `agent=`. The Initial User
Story Agent is never imported by the LLD package; where a test needs user-story
context it builds that stream via the real InitialUserStoryService (with a stub
agent) purely as test setup.
"""

import ast
from pathlib import Path

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.low_level_design.service import (
    LLDLockedError,
    LowLevelDesignService,
    NoFinalHLDError,
)
from app.agents.solution_architect.service import SolutionArchitectService


def _final_brd(stub_ba_agent, sow_file, sample_metadata, project_id="proj"):
    ba = BusinessAnalystService(project_id=project_id, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    return ba


def _sa_with_hld(ba, stub_sa_agent, *, finalize, project_id="proj"):
    sa = SolutionArchitectService(project_id=project_id, ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    if finalize:
        sa.choose_final_hld(1)
    return sa


def _lld(sa, ba, stub_lld_agent, project_id="proj"):
    return LowLevelDesignService(
        project_id=project_id, sa_service=sa, ba_service=ba, agent=stub_lld_agent
    )


# --- TEST 1: no HLD at all -> blocked -----------------------------------

def test_no_hld_blocks_lld_generation(stub_ba_agent, stub_lld_agent, sow_file, sample_metadata):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = SolutionArchitectService(project_id="proj", ba_service=ba)  # no HLD generated
    lld = _lld(sa, ba, stub_lld_agent)
    with pytest.raises(NoFinalHLDError):
        lld.generate_initial_lld()
    assert lld.has_versions() is False


# --- TEST 2: HLD exists but is a draft -> still blocked ---------------

def test_draft_hld_blocks_lld_generation(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=False)
    assert sa.get_final_hld() is None
    lld = _lld(sa, ba, stub_lld_agent)
    with pytest.raises(NoFinalHLDError):
        lld.generate_initial_lld()


# --- TEST 3: accepted HLD -> generation works, v1 created ------------

def test_final_hld_enables_lld_generation(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)

    v1 = lld.generate_initial_lld()

    assert v1.version == 1
    assert v1.source == "initial"
    assert v1.source_ref == "hld_v1"
    assert "**Version:** 1" in v1.content
    assert "**Version:** 0" not in v1.content
    hld_text_passed = stub_lld_agent.generate_calls[0][0]
    assert "High-Level Design" in hld_text_passed  # grounded in the HLD


def test_final_but_unlocked_hld_still_allowed(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    sa.unlock_final_hld()  # finalized-then-unlocked HLD still counts as accepted

    lld = _lld(sa, ba, stub_lld_agent)
    assert lld.generate_initial_lld().version == 1


# --- TEST 4: user stories absent -> LLD still generates -------------

def test_lld_generates_without_user_stories(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)

    v1 = lld.generate_initial_lld()

    assert v1.version == 1
    _, _, user_stories_text, _ = stub_lld_agent.generate_calls[0]
    assert user_stories_text == "(no draft user stories available)"
    assert "user stories" not in v1.note.lower()


# --- TEST 5: user stories available -> used as context -------------

def test_lld_uses_available_user_story_context(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    from app.agents.initial_user_story.service import InitialUserStoryService

    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    # test setup only — the LLD package itself never touches this service
    InitialUserStoryService(
        project_id="proj", ba_service=ba, agent=stub_us_agent
    ).generate_initial_stories()

    lld = _lld(sa, ba, stub_lld_agent)
    v1 = lld.generate_initial_lld()

    _, _, user_stories_text, _ = stub_lld_agent.generate_calls[0]
    assert "US-001" in user_stories_text
    assert "user stories" in v1.note.lower()


# --- TEST 6: LLD package does not import initial_user_story --------

def test_lld_package_does_not_import_user_story_agent():
    import app.agents.low_level_design.agent as lld_agent_mod
    import app.agents.low_level_design.service as lld_service_mod

    for mod in (lld_agent_mod, lld_service_mod):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert not any("initial_user_story" in name for name in imported), imported


# --- TEST 7: initial generation creates v1 ------------------------

def test_initial_generation_is_v1(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)
    assert lld.generate_initial_lld().version == 1
    assert len(lld.get_all_versions()) == 1


# --- TEST 8: manual edit -> new version, v1 untouched -----------

def test_manual_edit_creates_new_version(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)
    v1 = lld.generate_initial_lld()
    v1_content = v1.content

    v2 = lld.save_manual_edit(v1_content + "\n\n## 15. Hand added\n", note="hand edit")

    assert v2.version == 2
    assert v2.source == "manual_edit"
    assert lld.get_version(1).content == v1_content
    assert "Hand added" in lld.get_version(2).content


# --- TEST 9: AI refine -> new version, prior versions intact ---

def test_ai_refine_creates_new_version(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)
    v1 = lld.generate_initial_lld()
    v1_content = v1.content

    v2 = lld.refine_with_ai("Add an idempotency key to the registration endpoint.")

    assert v2.version == 2
    assert v2.source == "ai_refine"
    assert v2.note == "Add an idempotency key to the registration endpoint."
    assert lld.get_version(1).content == v1_content
    assert stub_lld_agent.refine_calls[0][0] == v1_content  # received the current LLD
    assert "**Version:** 2" in v2.content


# --- TEST 10: existing versions never overwritten -------------

def test_history_is_append_only(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)
    lld.generate_initial_lld()
    snapshot_after_v1 = [v.model_dump() for v in lld.get_all_versions()]

    lld.save_manual_edit(lld.get_version(1).content + "\nedit\n")
    lld.refine_with_ai("add a class")

    after = lld.get_all_versions()
    assert len(after) == 3
    assert [v.model_dump() for v in after][:1] == snapshot_after_v1
    assert [v.version for v in after] == [1, 2, 3]


# --- TEST 11: lock blocks edit + refine ---------------------

def test_locking_blocks_edit_and_refine(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)
    lld.generate_initial_lld()
    lld.choose_final_lld(1)

    assert lld.is_locked() is True
    with pytest.raises(LLDLockedError):
        lld.save_manual_edit("whatever")
    with pytest.raises(LLDLockedError):
        lld.refine_with_ai("whatever")


# --- TEST 12: unlock re-enables editing; history intact ---

def test_unlock_reenables_editing(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)
    lld.generate_initial_lld()
    lld.choose_final_lld(1)
    lld.unlock_final_lld()

    assert lld.is_locked() is False
    v2 = lld.save_manual_edit(lld.get_version(1).content + "\npost-unlock\n")
    assert v2.version == 2
    assert lld.get_final_lld().version == 1
    assert lld.get_final_lld().is_final is True
    assert len(lld.get_all_versions()) == 2


# --- TEST 15: HLD change after LLD -> flagged, not mutated ---

def test_hld_change_after_lld_is_flagged_not_mutated(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)
    lld.generate_initial_lld()
    lld_before = [v.model_dump() for v in lld.get_all_versions()]

    assert lld.hld_changed_since_lld() is False

    sa.unlock_final_hld()
    sa.save_manual_edit(sa.get_version(1).content + "\n\nNew component.\n")
    sa.choose_final_hld(2)

    assert lld.hld_changed_since_lld() is True
    assert lld.source_hld_version() == 1
    assert [v.model_dump() for v in lld.get_all_versions()] == lld_before


# --- TEST 16: BRD change does not mutate existing LLD content ---

def test_brd_change_does_not_mutate_lld(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)
    lld.generate_initial_lld()
    lld_before = [v.model_dump() for v in lld.get_all_versions()]

    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\n\nNew requirement.\n")
    ba.choose_final_brd(2)

    assert [v.model_dump() for v in lld.get_all_versions()] == lld_before


def test_metadata_is_derived_from_hld(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)
    lld.generate_initial_lld()

    _, _, _, metadata = stub_lld_agent.generate_calls[0]
    assert metadata.project_name == "Test Project"
    assert metadata.client_name == "Acme Corp"
    assert metadata.project_type == "Web Application"
