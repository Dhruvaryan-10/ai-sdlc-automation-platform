"""
Initial User Story service tests — the Phase 3 acceptance checklist.

All deterministic; the LLM is a stub injected via `agent=`. The Solution
Architect Agent is never constructed here — user story generation must not
depend on it (or on any HLD/LLD).
"""

import ast
from pathlib import Path

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import (
    InitialUserStoryService,
    NoFinalBRDError,
    UserStoryLockedError,
)


def _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata, project_id="proj"):
    ba = BusinessAnalystService(project_id=project_id, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    return ba


def _us(ba, stub_us_agent, project_id="proj"):
    return InitialUserStoryService(project_id=project_id, ba_service=ba, agent=stub_us_agent)


# --- TEST 1: no BRD at all -> blocked -------------------------------------

def test_no_brd_blocks_story_generation(stub_us_agent):
    ba = BusinessAnalystService(project_id="proj")
    us = _us(ba, stub_us_agent)
    with pytest.raises(NoFinalBRDError):
        us.generate_initial_stories()
    assert us.has_versions() is False


# --- TEST 2: BRD exists but is a draft -> still blocked -----------------

def test_draft_brd_blocks_story_generation(stub_ba_agent, stub_us_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    assert ba.get_final_brd() is None
    us = _us(ba, stub_us_agent)
    with pytest.raises(NoFinalBRDError):
        us.generate_initial_stories()


# --- TEST 3: accepted BRD -> generation works, v1 created --------------

def test_final_brd_enables_story_generation(stub_ba_agent, stub_us_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = _us(ba, stub_us_agent)

    v1 = us.generate_initial_stories()

    assert v1.version == 1
    assert v1.source == "initial"
    assert v1.source_ref == "brd_v1"
    assert "**Version:** 1" in v1.content
    assert "**Version:** 0" not in v1.content
    assert stub_us_agent.generate_calls, "story agent should have been invoked"
    brd_text_passed = stub_us_agent.generate_calls[0][0]
    assert "Business Requirement Document" in brd_text_passed  # grounded in the BRD


def test_final_but_unlocked_brd_still_allowed(
    stub_ba_agent, stub_us_agent, sow_file, sample_metadata
):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    ba.unlock_final_brd()  # finalized-then-unlocked BRD still counts as accepted

    us = _us(ba, stub_us_agent)
    assert us.generate_initial_stories().version == 1


# --- TEST 4: manual edit -> new version, v1 untouched ----------------

def test_manual_edit_creates_new_version(stub_ba_agent, stub_us_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = _us(ba, stub_us_agent)
    v1 = us.generate_initial_stories()
    v1_content = v1.content

    v2 = us.save_manual_edit(v1_content + "\n\n## US-009 — Hand added\n", note="hand edit")

    assert v2.version == 2
    assert v2.source == "manual_edit"
    assert us.get_version(1).content == v1_content
    assert "US-009" in us.get_version(2).content


# --- TEST 5: AI refine -> new version, prior versions intact --------

def test_ai_refine_creates_new_version(stub_ba_agent, stub_us_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = _us(ba, stub_us_agent)
    v1 = us.generate_initial_stories()
    v1_content = v1.content

    v2 = us.refine_with_ai("Split registration into email and social sign-up.")

    assert v2.version == 2
    assert v2.source == "ai_refine"
    assert v2.note == "Split registration into email and social sign-up."
    assert us.get_version(1).content == v1_content
    assert stub_us_agent.refine_calls[0][0] == v1_content  # received the current stories
    assert "**Version:** 2" in v2.content


# --- TEST 6: existing versions are never overwritten ----------------

def test_history_is_append_only(stub_ba_agent, stub_us_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = _us(ba, stub_us_agent)
    us.generate_initial_stories()
    snapshot_after_v1 = [v.model_dump() for v in us.get_all_versions()]

    us.save_manual_edit(us.get_version(1).content + "\nedit\n")
    us.refine_with_ai("add a story")

    after = us.get_all_versions()
    assert len(after) == 3
    assert [v.model_dump() for v in after][:1] == snapshot_after_v1  # v1 entry unchanged
    assert after[0].version == 1 and after[1].version == 2 and after[2].version == 3


# --- TEST 7: lock blocks edit + refine ---------------------------

def test_locking_blocks_edit_and_refine(stub_ba_agent, stub_us_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = _us(ba, stub_us_agent)
    us.generate_initial_stories()
    us.choose_final_stories(1)

    assert us.is_locked() is True
    with pytest.raises(UserStoryLockedError):
        us.save_manual_edit("whatever")
    with pytest.raises(UserStoryLockedError):
        us.refine_with_ai("whatever")


# --- TEST 8: unlock re-enables editing; history intact --------

def test_unlock_reenables_editing(stub_ba_agent, stub_us_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = _us(ba, stub_us_agent)
    us.generate_initial_stories()
    us.choose_final_stories(1)
    us.unlock_final_stories()

    assert us.is_locked() is False
    v2 = us.save_manual_edit(us.get_version(1).content + "\npost-unlock\n")
    assert v2.version == 2
    assert us.get_final_stories().version == 1
    assert us.get_final_stories().is_final is True
    assert len(us.get_all_versions()) == 2


# --- TEST 12: story generation does not require / touch the HLD -----

def test_story_generation_is_independent_of_solution_architect(
    stub_ba_agent, stub_us_agent, sow_file, sample_metadata
):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = _us(ba, stub_us_agent)

    # No SolutionArchitectService is constructed anywhere in this test.
    us.generate_initial_stories()

    versions_file = Path(us._version_service._versions_file)
    assert versions_file.parent.name == "user_stories"
    assert "hld" not in versions_file.parts

    # The package must not *import* anything from the solution_architect package
    # (prose mentions in docstrings are fine; actual import statements are not).
    import app.agents.initial_user_story.agent as us_agent_mod
    import app.agents.initial_user_story.service as us_service_mod

    for mod in (us_agent_mod, us_service_mod):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert not any("solution_architect" in name for name in imported), imported


# --- TEST 13: changing the BRD does not silently mutate the stories ---

def test_brd_change_after_stories_is_flagged_not_mutated(
    stub_ba_agent, stub_us_agent, sow_file, sample_metadata
):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = _us(ba, stub_us_agent)
    us.generate_initial_stories()
    stories_before = [v.model_dump() for v in us.get_all_versions()]

    assert us.brd_changed_since_stories() is False

    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\n\nNew requirement.\n")
    ba.choose_final_brd(2)

    assert us.brd_changed_since_stories() is True
    assert us.source_brd_version() == 1
    assert [v.model_dump() for v in us.get_all_versions()] == stories_before


def test_metadata_is_derived_from_final_brd(
    stub_ba_agent, stub_us_agent, sow_file, sample_metadata
):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = _us(ba, stub_us_agent)
    us.generate_initial_stories()

    _, metadata = stub_us_agent.generate_calls[0]
    assert metadata.project_name == "Test Project"
    assert metadata.client_name == "Acme Corp"
    assert metadata.project_type == "Web Application"
