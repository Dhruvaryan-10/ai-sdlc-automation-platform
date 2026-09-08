"""
Phase 11C — deterministic LLD generation-context reduction.

Covers the digest builder, the user-story index builder, the reduced-context
assembly, and the size-sanity fallback. All pure/deterministic — no Gemini, no
services. The end-to-end "the LLD service hands the agent a digest, not the full
BRD" checks live in test_low_level_design_service.py.
"""

from app.agents.low_level_design.context_reduction import (
    ReducedLLDContext,
    build_brd_requirements_digest,
    build_reduced_lld_context,
    build_user_story_index,
)
from app.agents.low_level_design.service import (
    _NO_BRD_SENTINEL,
    _NO_USER_STORIES_SENTINEL,
)

# A realistic multi-requirement BRD (FR/NFR/BR, unbold "FR-1. ..." form, with a
# wrapped continuation line on FR-2) plus one bold-titled requirement. The
# non-requirement prose (summary/scope/stakeholders) is what the digest strips.
BRD = """# Customer Support Portal — Business Requirement Document

**Version:** 3
**Client:** Acme Retail Group
**Project Type:** Web Application

## 1. Executive Summary
Acme Retail Group operates dozens of stores and an e-commerce site. Support
requests currently arrive by phone and a shared mailbox and are tracked in a
spreadsheet, which gives supervisors no real-time visibility and makes SLA
compliance impossible to measure. Acme requires a web-based Customer Support
Portal so authenticated customers can raise and track tickets, agents can triage
and resolve them against SLA timers, and supervisors can monitor backlog, SLA
breaches and CSAT. The portal must run on the client's existing cloud tenant,
integrate with corporate SSO for staff, and meet WCAG 2.1 AA. None of this
narrative is needed at implementation depth by the low-level design.

## 2. Scope
In scope: customer registration and login; ticket create / read / update;
threaded comments; file attachments; agent queues with filtering, assignment,
priority and SLA timers; a supervisor reporting dashboard; role-based access.
Out of scope: live chat, telephony integration, a native mobile app, and
multi-language support for this release.

## 3. Stakeholders
Retail operations (sponsor), support agents and supervisors (primary users),
customers (external users), IT security and IT operations.

## 6. Functional Requirements
FR-1. The system shall allow a customer to register with email and password and
verify the email address before first login.
FR-2. The system shall allow an authenticated customer to create a support ticket
with a subject, description, category, priority, and up to five attachments.
FR-3. The system shall let a customer view the status history of any ticket they own.
**FR-4: Ticket Comments** The system shall allow a customer to add a comment to an open ticket.

## 7. Non-Functional Requirements
NFR-1. 95th-percentile page load under 2 seconds at the expected concurrent load.
NFR-2. All data encrypted in transit (TLS 1.2+) and at rest.

## 8. Business Rules
BR-1. A customer may only view and modify tickets they created.
BR-2. A ticket cannot move to Closed unless it has first been Resolved.

## 9. Acceptance Criteria
- All functional requirements demonstrated end to end in UAT.
"""

USER_STORIES = """# Customer Support Portal — Draft User Stories

**Version:** 2
**Source:** Accepted BRD

## US-001 — Customer Registration

**User Story:**
As a customer, I want to create an account so that I can raise support tickets.

**Acceptance Criteria:**
- Required registration information can be entered.
- Invalid registration information is rejected.

**Priority:** High
**BRD Reference:** FR-1

## US-002 — Raise a Ticket

**User Story:**
As a customer, I want to submit a support ticket with attachments.

**Priority:** High
**BRD Reference:** FR-2, BR-1

## US-003 — Track Ticket

**User Story:**
As a customer, I want to see my ticket's status history.

**Priority:** Medium
**BRD Reference:** FR-3
"""


# --- 1. BRD Requirements Digest -----------------------------------------

def test_digest_retains_every_fr_nfr_br_id():
    digest = build_brd_requirements_digest(BRD)
    for rid in ("FR-1", "FR-2", "FR-3", "FR-4", "NFR-1", "NFR-2", "BR-1", "BR-2"):
        assert f"- {rid}:" in digest, f"{rid} missing from digest:\n{digest}"


def test_digest_groups_by_kind():
    digest = build_brd_requirements_digest(BRD)
    assert "Functional Requirements:" in digest
    assert "Non-Functional Requirements:" in digest
    assert "Business Rules:" in digest
    # FR block comes before the NFR block, which comes before the BR block
    assert (
        digest.index("Functional Requirements:")
        < digest.index("Non-Functional Requirements:")
        < digest.index("Business Rules:")
    )


def test_digest_retains_requirement_definition_lines():
    digest = build_brd_requirements_digest(BRD)
    # unbold "FR-1. ..." sentence, including its wrapped continuation
    assert "register with email and password" in digest
    assert "verify the email address before first login" in digest
    # wrapped FR-2 continuation folded into one line
    assert "up to five attachments" in digest
    # NFR / BR prose retained
    assert "95th-percentile page load under 2 seconds" in digest
    assert "only view and modify tickets they created" in digest
    # bold-titled "**FR-4: Ticket Comments**" keeps its title as the definition
    assert "- FR-4: Ticket Comments" in digest


def test_digest_is_smaller_than_full_brd():
    assert len(build_brd_requirements_digest(BRD)) < len(BRD)


def test_digest_empty_when_no_requirements():
    assert build_brd_requirements_digest("# Doc\n\nNo requirement ids here.\n") == ""


# --- 2. User Story Index ----------------------------------------------

def test_index_retains_every_story_id():
    index = build_user_story_index(USER_STORIES)
    for sid in ("US-001", "US-002", "US-003"):
        assert f"- {sid} " in index, f"{sid} missing from index:\n{index}"


def test_index_retains_brd_references():
    index = build_user_story_index(USER_STORIES)
    assert "- US-001 (BRD: FR-1)" in index
    assert "- US-002 (BRD: FR-2, BR-1)" in index
    assert "- US-003 (BRD: FR-3)" in index


def test_index_retains_story_goal():
    index = build_user_story_index(USER_STORIES)
    assert "Customer Registration" in index                     # heading title
    assert "I want to create an account" in index               # first body line
    assert "submit a support ticket with attachments" in index


def test_index_is_smaller_than_full_user_stories():
    assert len(build_user_story_index(USER_STORIES)) < len(USER_STORIES)


def test_index_empty_when_no_stories():
    assert build_user_story_index("# Draft User Stories\n\nNothing here yet.\n") == ""


# --- 3. reduced-context assembly -------------------------------------

def test_reduced_context_used_when_smaller():
    ctx = build_reduced_lld_context(
        BRD, USER_STORIES,
        no_brd_sentinel=_NO_BRD_SENTINEL,
        no_user_stories_sentinel=_NO_USER_STORIES_SENTINEL,
    )
    assert isinstance(ctx, ReducedLLDContext)
    assert ctx.reduced is True
    assert ctx.reduced_chars < ctx.full_chars
    # brd_block is the digest, not the full BRD prose
    assert "BRD REQUIREMENTS DIGEST" in ctx.brd_block
    assert "## 9. Acceptance Criteria" not in ctx.brd_block
    assert "Executive Summary" not in ctx.brd_block
    # user_stories_block is the index, not the full story bodies
    assert "USER STORY INDEX" in ctx.user_stories_block
    assert "**Acceptance Criteria:**" not in ctx.user_stories_block
    # every id still present through the reduced blocks
    for rid in ("FR-1", "FR-4", "NFR-2", "BR-2"):
        assert rid in ctx.brd_block
    for sid in ("US-001", "US-002", "US-003"):
        assert sid in ctx.user_stories_block


def test_reduced_context_passes_sentinels_through_untouched():
    ctx = build_reduced_lld_context(
        _NO_BRD_SENTINEL, _NO_USER_STORIES_SENTINEL,
        no_brd_sentinel=_NO_BRD_SENTINEL,
        no_user_stories_sentinel=_NO_USER_STORIES_SENTINEL,
    )
    assert ctx.brd_block == _NO_BRD_SENTINEL
    assert ctx.user_stories_block == _NO_USER_STORIES_SENTINEL
    assert ctx.reduced is False


def test_reduced_context_reduces_us_side_when_only_brd_absent():
    ctx = build_reduced_lld_context(
        _NO_BRD_SENTINEL, USER_STORIES,
        no_brd_sentinel=_NO_BRD_SENTINEL,
        no_user_stories_sentinel=_NO_USER_STORIES_SENTINEL,
    )
    assert ctx.brd_block == _NO_BRD_SENTINEL          # nothing to digest
    assert "USER STORY INDEX" in ctx.user_stories_block
    assert ctx.reduced is True
    assert ctx.reduced_chars < ctx.full_chars


# --- 4. size-sanity fallback -----------------------------------------

def test_fallback_to_full_context_when_reduced_is_not_smaller():
    # A BRD so terse its digest (header + parenthetical + bullets) is larger
    # than the source text itself.
    tiny_brd = "## 6. Functional Requirements\nFR-1. Do it.\nNFR-1. Fast.\nBR-1. Rule.\n"
    tiny_us = "## US-001 — X\n**BRD Reference:** FR-1\n"
    ctx = build_reduced_lld_context(
        tiny_brd, tiny_us,
        no_brd_sentinel=_NO_BRD_SENTINEL,
        no_user_stories_sentinel=_NO_USER_STORIES_SENTINEL,
    )
    assert ctx.reduced is False
    assert ctx.brd_block == tiny_brd                  # unchanged, byte-for-byte
    assert ctx.user_stories_block == tiny_us
    assert ctx.reduced_chars >= ctx.full_chars


def test_fallback_to_full_context_when_extract_captures_nothing():
    # Real BRD/US-sized text, but with no recognisable requirement / story ids:
    # the digest + index would silently drop everything -> keep the full text.
    no_ids_brd = "# Doc\n\n" + ("Business context paragraph. " * 40) + "\n"
    no_ids_us = "# Stories\n\n" + ("Some narrative about the users. " * 40) + "\n"
    ctx = build_reduced_lld_context(
        no_ids_brd, no_ids_us,
        no_brd_sentinel=_NO_BRD_SENTINEL,
        no_user_stories_sentinel=_NO_USER_STORIES_SENTINEL,
    )
    assert ctx.reduced is False
    assert ctx.brd_block == no_ids_brd
    assert ctx.user_stories_block == no_ids_us
