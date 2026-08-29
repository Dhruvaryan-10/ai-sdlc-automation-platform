"""
Solution Architect service tests — the Phase 2 acceptance checklist (TESTS 1-8, 11).

All deterministic; the LLM is a stub injected via `agent=`.
"""

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.solution_architect.service import (
    HLDLockedError,
    NoFinalBRDError,
    SolutionArchitectService,
)


def _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata, project_id="proj"):
    ba = BusinessAnalystService(project_id=project_id, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    return ba


def _sa(ba, stub_sa_agent, project_id="proj"):
    return SolutionArchitectService(project_id=project_id, ba_service=ba, agent=stub_sa_agent)


# --- TEST 1: no BRD at all -> blocked ---------------------------------------

def test_no_brd_blocks_hld(stub_sa_agent):
    ba = BusinessAnalystService(project_id="proj")
    sa = _sa(ba, stub_sa_agent)
    with pytest.raises(NoFinalBRDError):
        sa.generate_initial_hld()
    assert sa.has_versions() is False


# --- TEST 2: BRD exists but is a draft (not final) -> still blocked --------

def test_draft_brd_blocks_hld(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    assert ba.get_final_brd() is None  # draft only
    sa = _sa(ba, stub_sa_agent)
    with pytest.raises(NoFinalBRDError):
        sa.generate_initial_hld()


# --- TEST 3 + 4: accepting the BRD unlocks generation; HLD v1 created ------

def test_final_brd_enables_hld_generation(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    sa = _sa(ba, stub_sa_agent)

    hld_v1 = sa.generate_initial_hld()

    assert hld_v1.version == 1
    assert hld_v1.source == "initial"
    assert hld_v1.source_ref == "brd_v1"
    assert "**Version:** 1" in hld_v1.content          # stamped
    assert "**Version:** 0" not in hld_v1.content
    assert stub_sa_agent.generate_calls, "SA agent should have been invoked"
    # grounding: the agent received the final BRD text, not the SOW
    brd_text_passed = stub_sa_agent.generate_calls[0][0]
    assert "Business Requirement Document" in brd_text_passed


def test_final_but_unlocked_brd_still_allowed(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    ba.unlock_final_brd()  # decision #1: unlocked-but-final still counts as accepted

    sa = _sa(ba, stub_sa_agent)
    assert sa.generate_initial_hld().version == 1


# --- TEST 5: manual edit -> v2, v1 untouched ------------------------------

def test_manual_edit_creates_new_version(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    sa = _sa(ba, stub_sa_agent)
    v1 = sa.generate_initial_hld()
    v1_content = v1.content

    v2 = sa.save_manual_edit(v1_content + "\n\nEdited by hand.\n", note="hand edit")

    assert v2.version == 2
    assert v2.source == "manual_edit"
    assert sa.get_version(1).content == v1_content  # unchanged
    assert "Edited by hand." in sa.get_version(2).content


# --- TEST 6: AI refine -> new version, prior versions intact -------------

def test_ai_refine_creates_new_version(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    sa = _sa(ba, stub_sa_agent)
    v1 = sa.generate_initial_hld()
    v1_content = v1.content

    v2 = sa.refine_with_ai("Add a caching layer for frequently accessed product data.")

    assert v2.version == 2
    assert v2.source == "ai_refine"
    assert v2.note == "Add a caching layer for frequently accessed product data."
    assert sa.get_version(1).content == v1_content
    assert stub_sa_agent.refine_calls[0][0] == v1_content  # got the current HLD
    assert "**Version:** 2" in v2.content


# --- TEST 7: lock blocks edit + refine ---------------------------------

def test_locking_blocks_edit_and_refine(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    sa = _sa(ba, stub_sa_agent)
    sa.generate_initial_hld()
    sa.choose_final_hld(1)

    assert sa.is_locked() is True
    with pytest.raises(HLDLockedError):
        sa.save_manual_edit("whatever")
    with pytest.raises(HLDLockedError):
        sa.refine_with_ai("whatever")


# --- TEST 8: unlock re-enables editing; history intact ----------------

def test_unlock_reenables_editing(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    sa = _sa(ba, stub_sa_agent)
    sa.generate_initial_hld()
    sa.choose_final_hld(1)
    sa.unlock_final_hld()

    assert sa.is_locked() is False
    v2 = sa.save_manual_edit(sa.get_version(1).content + "\npost-unlock\n")
    assert v2.version == 2
    assert sa.get_final_hld().version == 1        # still marked final
    assert sa.get_final_hld().is_final is True
    assert len(sa.get_all_versions()) == 2        # nothing lost


# --- TEST 11: BRD changes after HLD exists ----------------------------

def test_brd_change_after_hld_is_flagged_not_mutated(
    stub_ba_agent, stub_sa_agent, sow_file, sample_metadata
):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    sa = _sa(ba, stub_sa_agent)
    sa.generate_initial_hld()
    hld_before = [v.model_dump() for v in sa.get_all_versions()]

    assert sa.brd_changed_since_hld() is False

    # user unlocks the BRD, edits it, and accepts the new version
    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\n\nNew requirement.\n")
    ba.choose_final_brd(2)

    assert sa.brd_changed_since_hld() is True
    assert sa.source_brd_version() == 1
    # the HLD stream is completely untouched — no invalidation, no deletion
    assert [v.model_dump() for v in sa.get_all_versions()] == hld_before


def test_metadata_is_derived_from_final_brd(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    ba = _ba_with_generated_brd(stub_ba_agent, sow_file, sample_metadata)
    ba.choose_final_brd(1)
    sa = _sa(ba, stub_sa_agent)
    sa.generate_initial_hld()

    _, metadata = stub_sa_agent.generate_calls[0]
    assert metadata.project_name == "Test Project"
    assert metadata.client_name == "Acme Corp"
    assert metadata.project_type == "Web Application"
