"""
Low-Level Design service tests — the Phase 4 acceptance checklist.

All deterministic; the LLM is a stub injected via `agent=`. The Initial User
Story Agent is never imported by the LLD package; where a test needs user-story
context it builds that stream via the real InitialUserStoryService (with a stub
agent) purely as test setup.
"""

import ast
import json
from pathlib import Path

import pytest

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import (
    LLDLockedError,
    LowLevelDesignService,
    NoFinalHLDError,
)
from app.agents.solution_architect.service import SolutionArchitectService
from app.services.version_service import VersionService
from app.utils.config import settings


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
    # Phase 10B: composite provenance — HLD (required) + BRD + User Stories
    # (context). No user stories were generated in this test, so us_vnone.
    assert v1.source_ref == "hld_v1;brd_v1;us_vnone"
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


# --- Phase 11C: LLD generation context is the digest + index, not full text ---

_REALISTIC_BRD = """# Support Portal — Business Requirement Document

**Version:** 1
**Client:** Acme Corp
**Project Type:** Web Application

## 1. Executive Summary
A long-form executive summary paragraph that exists only to pad the BRD so the
deterministic digest is meaningfully smaller than the full document. It repeats
context a technical LLD does not need verbatim. """ + ("Filler sentence. " * 40) + """

## 6. Functional Requirements
FR-1. The system shall allow a customer to register with email and password and
verify the email address before first login.
FR-2. The system shall allow a customer to create a support ticket with a
subject, description, category, priority and up to five attachments.
FR-3. The system shall let an agent change a ticket's status through the
lifecycle New -> Open -> Pending -> Resolved -> Closed.

## 7. Non-Functional Requirements
NFR-1. 95th-percentile page load under 2 seconds at expected concurrent load.
NFR-2. All data encrypted in transit and at rest.

## 8. Business Rules
BR-1. A customer may only view and modify tickets they created.
BR-2. A ticket cannot move to Closed unless it has first been Resolved.
"""

_REALISTIC_USER_STORIES = """# Support Portal — Draft User Stories

**Version:** 1
**Source:** Accepted BRD

## US-001 — Customer Registration

**User Story:**
As a customer, I want to create an account so that I can raise support tickets.

**Acceptance Criteria:**
- Required registration information can be entered.
- Invalid registration information is rejected.
- """ + ("A padding acceptance-criterion line. " * 20) + """

**Priority:** High
**BRD Reference:** FR-1

## US-002 — Raise a Ticket

**User Story:**
As a customer, I want to submit a support ticket with attachments.

**Acceptance Criteria:**
- """ + ("Another padding acceptance-criterion line. " * 20) + """

**Priority:** High
**BRD Reference:** FR-2, BR-1
"""


class _FixedBRDAgent:
    def __init__(self, brd):
        self._brd = brd
        self.generate_calls = []

    def generate_brd(self, clean_sow, metadata):
        self.generate_calls.append((clean_sow, metadata))
        return self._brd

    def refine_brd(self, current_brd, user_feedback, current_version):  # pragma: no cover
        return current_brd


class _FixedUserStoryAgent:
    def __init__(self, stories):
        self._stories = stories
        self.generate_calls = []

    def generate_stories(self, brd_text, metadata):
        self.generate_calls.append((brd_text, metadata))
        return self._stories

    def refine_stories(self, current_stories, user_feedback, current_version):  # pragma: no cover
        return current_stories


def _stack_with(brd_text, us_text, stub_sa_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id="proj", agent=_FixedBRDAgent(brd_text))
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    if us_text is not None:
        InitialUserStoryService(
            project_id="proj", ba_service=ba, agent=_FixedUserStoryAgent(us_text)
        ).generate_initial_stories()
    return ba, sa


def test_lld_generation_receives_digest_and_index_not_full_brd_or_stories(
    stub_sa_agent, stub_lld_agent, sow_file, sample_metadata
):
    from app.agents.low_level_design.context_reduction import (
        build_brd_requirements_digest,
        build_user_story_index,
    )

    ba, sa = _stack_with(
        _REALISTIC_BRD, _REALISTIC_USER_STORIES, stub_sa_agent, sow_file, sample_metadata
    )
    lld = _lld(sa, ba, stub_lld_agent)
    lld.generate_initial_lld()

    hld_text, brd_text, user_stories_text, _ = stub_lld_agent.generate_calls[0]

    # full HLD is passed unchanged
    assert "High-Level Design" in hld_text
    assert hld_text == sa.get_final_hld().content

    # BRD block is the deterministic digest, not the full BRD document
    assert brd_text == build_brd_requirements_digest(ba.get_final_brd().content)
    assert "BRD REQUIREMENTS DIGEST" in brd_text
    assert "## 1. Executive Summary" not in brd_text
    assert "Filler sentence." not in brd_text
    assert len(brd_text) < len(ba.get_final_brd().content)
    for rid in ("FR-1", "FR-2", "FR-3", "NFR-1", "NFR-2", "BR-1", "BR-2"):
        assert rid in brd_text

    # User-story block is the deterministic index, not the full story document
    assert user_stories_text == build_user_story_index(_REALISTIC_USER_STORIES)
    assert "USER STORY INDEX" in user_stories_text
    assert "**Acceptance Criteria:**" not in user_stories_text
    assert "padding acceptance-criterion line" not in user_stories_text
    assert "US-001" in user_stories_text and "US-002" in user_stories_text
    assert "(BRD: FR-2, BR-1)" in user_stories_text


def test_lld_generation_context_matches_the_deterministic_size_guard(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    """Whatever the size guard decides (reduced digest+index, or full-text
    fallback), the LLD service must hand the agent exactly that — and the ids
    always survive. STUB_BRD / STUB_USER_STORIES are tiny, so this exercises the
    small-input path end to end without asserting which branch wins."""
    from app.agents.low_level_design.context_reduction import build_reduced_lld_context
    from app.agents.low_level_design.service import (
        _NO_BRD_SENTINEL,
        _NO_USER_STORIES_SENTINEL,
    )

    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    InitialUserStoryService(
        project_id="proj", ba_service=ba, agent=stub_us_agent
    ).generate_initial_stories()

    lld = _lld(sa, ba, stub_lld_agent)
    lld.generate_initial_lld()
    _, brd_text, user_stories_text, _ = stub_lld_agent.generate_calls[0]

    us_content = (
        VersionService(project_id="proj", subdir="user_stories")
        .get_latest_version()
        .content
    )
    ctx = build_reduced_lld_context(
        ba.get_final_brd().content,
        us_content,
        no_brd_sentinel=_NO_BRD_SENTINEL,
        no_user_stories_sentinel=_NO_USER_STORIES_SENTINEL,
    )
    assert brd_text == ctx.brd_block
    assert user_stories_text == ctx.user_stories_block
    if not ctx.reduced:                                   # fallback: untouched source
        assert brd_text == ba.get_final_brd().content
        assert user_stories_text == us_content
    assert "FR-1" in brd_text
    assert "US-001" in user_stories_text


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


# ============================================================
# Phase 10B — LLD composite provenance + staleness
# ============================================================

def _us(ba, stub_us_agent):
    us = InitialUserStoryService(project_id="proj", ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()
    return us


def test_lld_records_composite_provenance_with_user_stories(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    _us(ba, stub_us_agent)                                  # US v1 present
    lld = _lld(sa, ba, stub_lld_agent)
    v1 = lld.generate_initial_lld()

    assert v1.source_ref == "hld_v1;brd_v1;us_v1"
    assert lld.recorded_source_versions() == {"hld": 1, "brd": 1, "us": 1}
    assert lld.source_hld_version() == 1
    assert lld.stale_sources() == []
    assert lld.is_stale() is False


def test_lld_legacy_hld_only_source_ref_is_parsed_and_never_flags_brd_or_us(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    """A pre-10B project stored `source_ref="hld_v1"`. The parser must accept it,
    and a later BRD / user-story change must NOT flag BRD/US stale (they were
    never recorded), only an HLD change."""
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    lld = _lld(sa, ba, stub_lld_agent)
    lld.generate_initial_lld()

    # simulate a legacy record: rewrite v1's source_ref to the old HLD-only form
    vfile = Path(settings.resolved_output_dir()) / "proj" / "lld" / "versions.json"
    data = json.loads(vfile.read_text(encoding="utf-8"))
    data[0]["source_ref"] = "hld_v1"
    vfile.write_text(json.dumps(data, indent=2), encoding="utf-8")

    assert lld.recorded_source_versions() == {"hld": 1, "brd": None, "us": None}

    # BRD changes + user stories added afterwards -> NOT stale (never recorded)
    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\n\nNew req.\n")
    ba.choose_final_brd(2)
    _us(ba, stub_us_agent)
    assert lld.stale_sources() == []

    # but an HLD change still flags HLD
    sa.unlock_final_hld()
    sa.save_manual_edit(sa.get_version(1).content + "\n\nNew component.\n")
    sa.choose_final_hld(2)
    assert lld.stale_sources() == ["HLD"]
    assert lld.hld_changed_since_lld() is True


def test_lld_staleness_flags_brd_hld_and_user_stories_when_recorded(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = _sa_with_hld(ba, stub_sa_agent, finalize=True)
    us = _us(ba, stub_us_agent)
    lld = _lld(sa, ba, stub_lld_agent)
    lld.generate_initial_lld()
    lld_before = [v.model_dump() for v in lld.get_all_versions()]
    assert lld.stale_sources() == []

    # user stories refined to v2 -> "User Stories" stale (LLD not mutated)
    us.save_manual_edit(us.get_version(1).content + "\nextra\n")
    assert lld.stale_sources() == ["User Stories"]

    # BRD re-finalized -> BRD also stale
    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\n\nNew req.\n")
    ba.choose_final_brd(2)
    assert set(lld.stale_sources()) == {"BRD", "User Stories"}

    # HLD re-finalized -> all three
    sa.unlock_final_hld()
    sa.save_manual_edit(sa.get_version(1).content + "\n\nNew component.\n")
    sa.choose_final_hld(2)
    assert set(lld.stale_sources()) == {"BRD", "HLD", "User Stories"}
    assert lld.is_stale() is True
    # nothing was regenerated or mutated
    assert [v.model_dump() for v in lld.get_all_versions()] == lld_before
