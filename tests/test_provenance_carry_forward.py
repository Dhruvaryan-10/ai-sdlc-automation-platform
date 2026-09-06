"""
Phase 10B / Part D — provenance carry-forward on non-initial versions.

Before 10B, HLD / Initial User Stories / LLD dropped `source_ref` (-> None) on
every manual edit / freeform AI refine, so only v1 carried provenance. They now
carry the prior `source_ref` forward for a plain edit / freeform refine (the
edit does not change which upstream artifact the version is based on).

  * HLD  : simple `brd_v{n}` carried forward.
  * LLD  : composite `hld_v{h};brd_v{b};us_v{u}` carried forward.
  * Initial User Stories: simple `brd_v{n}` carried forward; a Phase 5 COMPOSITE
    `source_ref` is deliberately NOT carried (it belongs to
    UserStoryRefinementService and would mislabel a Phase 3 edit).

Deterministic; stub agents.
"""

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.user_story_refinement.service import UserStoryRefinementService

PID = "p10b_prov"


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


# ============================ HLD ============================

def test_hld_manual_edit_and_refine_carry_source_ref_forward(
    stub_ba_agent, stub_sa_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = SolutionArchitectService(project_id=PID, ba_service=ba, agent=stub_sa_agent)
    v1 = sa.generate_initial_hld()
    assert v1.source_ref == "brd_v1"

    v2 = sa.save_manual_edit(sa.get_version(1).content + "\n\nEdit.\n")
    assert v2.source == "manual_edit"
    assert v2.source_ref == "brd_v1"          # was None before 10B

    v3 = sa.refine_with_ai("add a caching layer")
    assert v3.source == "ai_refine"
    assert v3.source_ref == "brd_v1"          # was None before 10B
    assert sa.source_brd_version() == 1


# ==================== Initial User Stories ====================

def test_initial_user_story_edit_and_refine_carry_simple_source_ref_forward(
    stub_ba_agent, stub_us_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    v1 = us.generate_initial_stories()
    assert v1.source_ref == "brd_v1"

    v2 = us.save_manual_edit(us.get_version(1).content + "\n\nEdit.\n")
    assert v2.source_ref == "brd_v1"          # was None before 10B

    v3 = us.refine_with_ai("add a password-reset story")
    assert v3.source_ref == "brd_v1"          # was None before 10B


def test_initial_user_story_does_not_carry_a_phase5_composite_source_ref(
    stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata
):
    """After a Phase 5 artifact refinement (composite source_ref), a Phase 3
    freeform edit/refine must NOT inherit that composite — otherwise the new
    version would be mislabelled as an 'Artifact Refinement'."""
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    usr = UserStoryRefinementService(project_id=PID, agent=stub_usr_agent)
    v2 = usr.refine()
    assert ";" in (v2.source_ref or "")       # composite (Phase 5)

    v3 = us.save_manual_edit(us.get_version(2).content + "\n\nEdit.\n")
    assert v3.source_ref is None              # composite deliberately dropped
    v4 = us.refine_with_ai("tweak wording")
    assert v4.source_ref is None


# ============================ LLD ============================

def test_lld_manual_edit_and_refine_carry_composite_source_ref_forward(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _final_hld(ba, stub_sa_agent)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()             # US v1 -> LLD records us_v1

    lld = LowLevelDesignService(project_id=PID, sa_service=sa, ba_service=ba, agent=stub_lld_agent)
    v1 = lld.generate_initial_lld()
    assert v1.source_ref == "hld_v1;brd_v1;us_v1"

    v2 = lld.save_manual_edit(lld.get_version(1).content + "\n\nEdit.\n")
    assert v2.source == "manual_edit"
    assert v2.source_ref == "hld_v1;brd_v1;us_v1"   # was None before 10B

    v3 = lld.refine_with_ai("add a caching table")
    assert v3.source == "ai_refine"
    assert v3.source_ref == "hld_v1;brd_v1;us_v1"   # was None before 10B
    assert lld.recorded_source_versions() == {"hld": 1, "brd": 1, "us": 1}
