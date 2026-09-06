"""
Phase 10B / Part F — `generate_initial_*` service-contract hardening.

The four `generate_initial_*` methods used to hard-code the in-document
`**Version:**` line to 1 and had no lock guard. They now:
  * stamp the ACTUAL next version number,
  * refuse to run through a locked final artifact,
  * keep append-only history (a repeat call creates v2, never overwrites v1).

Deterministic; the LLM is a stub injected via `agent=`. The orchestration
graph's own idempotency guard is unchanged and covered elsewhere.
"""

import pytest

from app.agents.business_analyst.service import BRDLockedError, BusinessAnalystService
from app.agents.initial_user_story.service import (
    InitialUserStoryService,
    UserStoryLockedError,
)
from app.agents.low_level_design.service import LLDLockedError, LowLevelDesignService
from app.agents.solution_architect.service import (
    HLDLockedError,
    SolutionArchitectService,
)

PID = "p10b_initial"


def _brd(stub_ba_agent, sow_file, sample_metadata, *, finalize=True):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    if finalize:
        ba.choose_final_brd(1)
    return ba


def _hld(ba, stub_sa_agent, *, finalize=True):
    sa = SolutionArchitectService(project_id=PID, ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    if finalize:
        sa.choose_final_hld(1)
    return sa


# ============================ BRD ============================

def test_brd_first_generation_stamps_v1(stub_ba_agent, sow_file, sample_metadata):
    ba = _brd(stub_ba_agent, sow_file, sample_metadata, finalize=False)
    v1 = ba.get_version(1)
    assert v1.version == 1
    assert "**Version:** 1" in v1.content


def test_brd_second_direct_call_creates_v2_with_correct_stamp(
    stub_ba_agent, sow_file, sample_metadata
):
    ba = _brd(stub_ba_agent, sow_file, sample_metadata, finalize=False)
    v1_content = ba.get_version(1).content

    v2 = ba.generate_initial_brd(sow_file, sample_metadata)   # direct repeat call

    assert v2.version == 2
    assert "**Version:** 2" in v2.content
    assert "**Version:** 1" not in v2.content          # no malformed v1 stamp on v2
    assert ba.get_version(1).content == v1_content     # append-only: v1 untouched
    assert [v.version for v in ba.get_all_versions()] == [1, 2]


def test_brd_generate_initial_refused_when_final_is_locked(
    stub_ba_agent, sow_file, sample_metadata
):
    ba = _brd(stub_ba_agent, sow_file, sample_metadata, finalize=True)  # v1 final + locked
    assert ba.is_locked() is True
    with pytest.raises(BRDLockedError):
        ba.generate_initial_brd(sow_file, sample_metadata)
    assert [v.version for v in ba.get_all_versions()] == [1]           # no mutation

    ba.unlock_final_brd()
    v2 = ba.generate_initial_brd(sow_file, sample_metadata)
    assert v2.version == 2 and "**Version:** 2" in v2.content
    assert ba.get_final_brd().version == 1                             # final unchanged


# ============================ HLD ============================

def test_hld_second_direct_call_and_lock_guard(
    stub_ba_agent, stub_sa_agent, sow_file, sample_metadata
):
    ba = _brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _hld(ba, stub_sa_agent, finalize=False)
    v1_content = sa.get_version(1).content

    v2 = sa.generate_initial_hld()
    assert v2.version == 2 and "**Version:** 2" in v2.content
    assert "**Version:** 1" not in v2.content
    assert sa.get_version(1).content == v1_content
    assert [v.version for v in sa.get_all_versions()] == [1, 2]

    sa.choose_final_hld(2)
    with pytest.raises(HLDLockedError):
        sa.generate_initial_hld()
    assert [v.version for v in sa.get_all_versions()] == [1, 2]


# ==================== Initial User Stories ====================

def test_user_stories_second_direct_call_and_lock_guard(
    stub_ba_agent, stub_us_agent, sow_file, sample_metadata
):
    ba = _brd(stub_ba_agent, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()
    v1_content = us.get_version(1).content

    v2 = us.generate_initial_stories()
    assert v2.version == 2 and "**Version:** 2" in v2.content
    assert "**Version:** 1" not in v2.content
    assert us.get_version(1).content == v1_content
    assert [v.version for v in us.get_all_versions()] == [1, 2]

    us.choose_final_stories(2)
    with pytest.raises(UserStoryLockedError):
        us.generate_initial_stories()
    assert [v.version for v in us.get_all_versions()] == [1, 2]


# ============================ LLD ============================

def test_lld_second_direct_call_and_lock_guard(
    stub_ba_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _hld(ba, stub_sa_agent)
    lld = LowLevelDesignService(project_id=PID, sa_service=sa, ba_service=ba, agent=stub_lld_agent)
    lld.generate_initial_lld()
    v1_content = lld.get_version(1).content

    v2 = lld.generate_initial_lld()
    assert v2.version == 2 and "**Version:** 2" in v2.content
    assert "**Version:** 1" not in v2.content
    assert lld.get_version(1).content == v1_content
    assert [v.version for v in lld.get_all_versions()] == [1, 2]

    lld.choose_final_lld(2)
    with pytest.raises(LLDLockedError):
        lld.generate_initial_lld()
    assert [v.version for v in lld.get_all_versions()] == [1, 2]
