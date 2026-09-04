"""
Regression coverage for the LangChain structured-output pilot (report item R6).

Covers, with no real Gemini call:
  1. TestCaseAgent._invoke_structured converts a Pydantic ValidationError into
     TestCaseAgentError.
  2. None structured response is rejected.
  3. Empty test_cases list is rejected.
  4. TestCaseAgent and the agent TestCaseService builds default to structured=True.
  5. A legacy-style persisted Test Case BRDVersion still loads, hand-edits,
     refines and exports to DOCX without touching the original record.
  6. Structured output preserves non-ASCII characters (ensure_ascii=False).
"""

import json

import pytest
from pydantic import ValidationError

from app.agents.test_case.agent import TestCaseAgent, TestCaseAgentError
from app.agents.test_case.schema import TestCaseList, TestCaseModel
from app.agents.test_case.service import TestCaseService
from app.document_generator.brd_generator import generate_test_cases_docx
from app.services.version_service import VersionService

PID = "legacy-proj"


# --- R6.1 / R6.2 / R6.3: _invoke_structured guards -------------------------

def _agent_with_fake_structured(fake):
    agent = TestCaseAgent(structured=True)  # constructs; no network at init
    agent._structured_llm = fake
    return agent


def test_invoke_structured_wraps_pydantic_validation_error():
    class _Raises:
        def invoke(self, prompt):
            # A real Pydantic failure, exactly like langchain's structured parse.
            return TestCaseList(**{"test_cases": [{"id": "NOT-A-TC-ID"}]})

    agent = _agent_with_fake_structured(_Raises())
    with pytest.raises(TestCaseAgentError):
        agent._invoke_structured("prompt")
    with pytest.raises(TestCaseAgentError):
        agent._run("prompt")


def test_invoke_structured_rejects_none():
    class _NoneLLM:
        def invoke(self, prompt):
            return None

    agent = _agent_with_fake_structured(_NoneLLM())
    with pytest.raises(TestCaseAgentError):
        agent._invoke_structured("prompt")


def test_invoke_structured_rejects_empty_test_cases():
    class _EmptyLLM:
        def invoke(self, prompt):
            # bypass min_length validation to reach the agent-side guard
            return TestCaseList.model_construct(test_cases=[])

    agent = _agent_with_fake_structured(_EmptyLLM())
    with pytest.raises(TestCaseAgentError):
        agent._invoke_structured("prompt")


# --- R6.4: structured=True is the default -------------------------------

def test_testcaseagent_defaults_to_structured_true():
    agent = TestCaseAgent()
    assert agent._structured is True
    assert agent._structured_llm is not None


def test_testcaseservice_builds_a_structured_agent_by_default():
    svc = TestCaseService(project_id=PID)
    assert isinstance(svc._agent, TestCaseAgent)
    assert svc._agent._structured is True
    assert svc._agent._structured_llm is not None


# --- R6.6: non-ASCII survives the structured -> JSON string hop ------------

def test_structured_output_preserves_non_ascii():
    case = TestCaseModel(
        id="TC-001",
        title="Vérifier le résumé — flèche → caractères Unicode",
        requirement_or_story_ref="FR-1",
        test_steps=["Ouvrir la página", "Enregistrer"],
        expected_result="Le résumé s'affiche correctement",
        priority="Haute",
        test_type="Functional",
    )

    class _Fake:
        def invoke(self, prompt):
            return TestCaseList(test_cases=[case])

    agent = _agent_with_fake_structured(_Fake())
    out = agent._run("prompt")
    assert isinstance(out, str)
    assert "résumé" in out and "flèche" in out and "→" in out and "—" in out
    assert "\\u" not in out  # not ASCII-escaped
    assert json.loads(out)["test_cases"][0]["title"].startswith("Vérifier")


# --- R6.5: legacy persisted Test Case version still works ------------------

_LEGACY_BRD = """# Legacy Project — Business Requirement Document

**Version:** 1
**Client:** Legacy Corp
**Project Type:** Web Application

## 8. Functional Requirements
FR-1. The system shall let a customer register an account.
"""

# Pre-R1/R2 shape: bullet-wrapped, self-numbered steps; no traceability line
# style guarantees. This is what an old test_cases/versions.json record holds.
_LEGACY_TC_DOC = """# Legacy Project — Test Cases

**Version:** 1
**Source:** Generated from artifacts
**Built From:** BRD v1, HLD unavailable, LLD unavailable, User Stories unavailable
**Client:** Legacy Corp
**Project Type:** Web Application

## TC-001 — Register a new customer account

**BRD Reference:** FR-1
**Priority:** High
**Test Type:** Functional

**Preconditions:**
- The registration page is reachable.

**Test Steps:**
- 1. Open the registration page.
- 2. Enter valid details and submit.

**Expected Result:**
A new account is created.
"""


@pytest.fixture
def legacy_streams(isolated_output_dir):
    """Seed a final BRD and a pre-pilot test_cases v1 straight through VersionService."""
    brd = VersionService(project_id=PID)
    brd.add_version(content=_LEGACY_BRD, source="initial")
    brd.mark_final(1)

    tc = VersionService(project_id=PID, subdir="test_cases")
    tc.add_version(
        content=_LEGACY_TC_DOC,
        source="initial",
        note="Generated from BRD v1 (HLD, LLD, User Stories: unavailable)",
        source_ref="brd_v1;hld_vnone;lld_vnone;us_vnone",
    )
    return tc


def test_legacy_version_loads_unchanged(legacy_streams):
    svc = TestCaseService(project_id=PID)
    versions = svc.get_all_versions()
    assert len(versions) == 1
    assert versions[0].content == _LEGACY_TC_DOC
    assert versions[0].source_ref == "brd_v1;hld_vnone;lld_vnone;us_vnone"


def test_legacy_version_can_be_manually_edited_without_data_loss(legacy_streams):
    svc = TestCaseService(project_id=PID)
    original = svc.get_version(1).content

    edited = _LEGACY_TC_DOC.replace("A new account is created.",
                                   "A new account is created and the user is signed in.")
    v2 = svc.save_manual_edit(edited, note="tighten expected result")

    assert v2.version == 2 and v2.source == "manual_edit"
    assert svc.get_version(1).content == original  # append-only, untouched
    assert "signed in" in svc.get_version(2).content


def test_legacy_version_can_be_refined_without_data_loss(legacy_streams, stub_tc_agent):
    svc = TestCaseService(project_id=PID, agent=stub_tc_agent)
    original = svc.get_version(1).content

    v2 = svc.refine_with_ai("clarify the TC-002 title")

    assert v2.version == 2
    assert svc.get_version(1).content == original  # original record intact
    assert "## TC-001 — " in v2.content
    assert stub_tc_agent.refine_calls  # the legacy doc was passed in as context


def test_legacy_version_exports_to_docx(legacy_streams, tmp_path):
    from docx import Document

    svc = TestCaseService(project_id=PID)
    out = tmp_path / "TestCases_v1.docx"
    generate_test_cases_docx(svc.get_version(1).content, out)

    doc = Document(str(out))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "TC-001 — Register a new customer account" in text
    assert "A new account is created." in text
