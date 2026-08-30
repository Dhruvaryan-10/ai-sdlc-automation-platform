"""
Shared test fixtures and stub LLM agents.

These tests are deterministic and NEVER call Gemini. Every place a real agent
would hit the API, a stub is injected via the constructor `agent=` parameter
that BusinessAnalystService and SolutionArchitectService already expose for
exactly this purpose. Real generation/refinement is covered by manual
integration testing through the Streamlit UI.
"""

import os
import tempfile

# Settings() validates required env at import time, and get_logger() opens a log
# file under LOG_DIR the first time any app module is imported. Set both BEFORE
# importing anything from `app`.
os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="ba-agent-test-logs-"))

import pytest  # noqa: E402

from app.agents.business_analyst.agent import ProjectMetadata  # noqa: E402
from app.utils.config import settings  # noqa: E402


# --- isolation: every test gets its own outputs/ directory --------------------

@pytest.fixture(autouse=True)
def isolated_output_dir(tmp_path, monkeypatch):
    """Point VersionService persistence at a per-test temp directory."""
    out = tmp_path / "outputs"
    monkeypatch.setattr(settings, "output_dir", str(out))
    return out


# --- stub agents ------------------------------------------------------------

STUB_BRD = """# Test Project — Business Requirement Document

**Version:** 0
**Generated Date:** 2026-01-01
**Client:** Acme Corp
**Project Type:** Web Application

## 1. Executive Summary
A short summary produced by the stub BA agent for testing.

## 8. Functional Requirements
FR-1. The system shall do the thing.
"""

STUB_HLD = """# Test Project — High-Level Design

**Version:** 0
**Generated Date:** 2026-01-01
**Client:** Acme Corp
**Project Type:** Web Application
**Source:** Accepted BRD

## 1. Architecture Overview
A layered web application described at a high level by the stub SA agent.

## 2. Architecture Diagram / Architecture Description
Client -> API -> Service -> Data Store

## 11. Assumptions and Architectural Decisions
**Assumption:** relational storage, pending confirmation.
"""

STUB_USER_STORIES = """# Test Project — Draft User Stories

**Version:** 0
**Generated Date:** 2026-01-01
**Client:** Acme Corp
**Project Type:** Web Application
**Source:** Accepted BRD

## US-001 — Customer Registration

**User Story:**
As a customer,
I want to create an account,
so that I can access the application.

**Acceptance Criteria:**
- Required registration information can be entered.
- Invalid registration information is rejected.
- A valid account can be created.

**Priority:** High
**BRD Reference:** FR-1
"""

STUB_LLD = """# Test Project — Low-Level Design

**Version:** 0
**Generated Date:** 2026-01-01
**Client:** Acme Corp
**Project Type:** Web Application
**Source:** Accepted HLD

## 1. Introduction and Source Traceability
Detailed design derived from the stub HLD for testing.

## 3. Classes, Responsibilities and Interfaces
- RegistrationService: creates and validates customer accounts.

## 9. Error Handling
Invalid input returns a 400 with a machine-readable error code.
"""


class StubBAAgent:
    """Stands in for BusinessAnalystAgent. Returns canned markdown."""

    def __init__(self):
        self.generate_calls = []
        self.refine_calls = []

    def generate_brd(self, clean_sow: str, metadata: ProjectMetadata) -> str:
        self.generate_calls.append((clean_sow, metadata))
        return STUB_BRD

    def refine_brd(self, current_brd: str, user_feedback: str, current_version: int) -> str:
        self.refine_calls.append((current_brd, user_feedback, current_version))
        return current_brd + f"\n\n## Refinement\n{user_feedback}\n"


class StubSAAgent:
    """Stands in for SolutionArchitectAgent. Returns canned markdown."""

    def __init__(self):
        self.generate_calls = []
        self.refine_calls = []

    def generate_hld(self, brd_text: str, metadata: ProjectMetadata) -> str:
        self.generate_calls.append((brd_text, metadata))
        return STUB_HLD

    def refine_hld(self, current_hld: str, user_feedback: str, current_version: int) -> str:
        self.refine_calls.append((current_hld, user_feedback, current_version))
        return current_hld + f"\n\n## 12. Caching\n{user_feedback}\n"


class StubUserStoryAgent:
    """Stands in for InitialUserStoryAgent. Returns canned markdown."""

    def __init__(self):
        self.generate_calls = []
        self.refine_calls = []

    def generate_stories(self, brd_text: str, metadata: ProjectMetadata) -> str:
        self.generate_calls.append((brd_text, metadata))
        return STUB_USER_STORIES

    def refine_stories(
        self, current_stories: str, user_feedback: str, current_version: int
    ) -> str:
        self.refine_calls.append((current_stories, user_feedback, current_version))
        return current_stories + f"\n\n## US-002 — Added Story\n{user_feedback}\n"


class StubLLDAgent:
    """Stands in for LowLevelDesignAgent. Returns canned markdown."""

    def __init__(self):
        self.generate_calls = []
        self.refine_calls = []

    def generate_lld(
        self,
        hld_text: str,
        brd_text: str,
        user_stories_text: str,
        metadata: ProjectMetadata,
    ) -> str:
        self.generate_calls.append((hld_text, brd_text, user_stories_text, metadata))
        return STUB_LLD

    def refine_lld(self, current_lld: str, user_feedback: str, current_version: int) -> str:
        self.refine_calls.append((current_lld, user_feedback, current_version))
        return current_lld + f"\n\n## 14. Addendum\n{user_feedback}\n"


@pytest.fixture
def stub_ba_agent():
    return StubBAAgent()


@pytest.fixture
def stub_sa_agent():
    return StubSAAgent()


@pytest.fixture
def stub_us_agent():
    return StubUserStoryAgent()


@pytest.fixture
def stub_lld_agent():
    return StubLLDAgent()


@pytest.fixture
def sample_metadata():
    return ProjectMetadata(
        project_name="Test Project",
        client_name="Acme Corp",
        project_type="Web Application",
        industry="Retail",
    )


@pytest.fixture
def sow_file(tmp_path):
    """A minimal text SOW on disk for BusinessAnalystService.generate_initial_brd."""
    path = tmp_path / "sow.txt"
    path.write_text(
        "Statement of Work\n\nBuild a customer web portal with search and checkout.\n",
        encoding="utf-8",
    )
    return path
