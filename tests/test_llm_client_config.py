"""
Phase 11A / Items 2 + 3 — lazy Gemini-client construction and explicit,
configurable timeout / retry policy.

Reconnaissance measured `ChatGoogleGenerativeAI()` at ~1.3 s (it builds two SSL
contexts) with NO network call, and every service `__init__` used to pay that
even on read-only paths. These tests:

  * pin the new `settings.gemini_timeout_seconds` / `settings.gemini_max_retries`
    defaults (conservative; well above the ~426 s legit LLD latency observed),
  * prove `build_chat_llm()` passes them explicitly (previously implicit SDK
    defaults: no timeout, `max_retries=6`), and model/temperature are unchanged,
  * prove NOTHING builds a client until a real generate/refine call — every
    agent, every service, `sdlc_status()`, and the quality/traceability reports.

Deterministic; `ChatGoogleGenerativeAI` is stubbed, so no SSL/network cost.
"""

import pytest

import app.utils.llm as llm_mod
from app.utils.config import Settings, settings
from app.utils.llm import build_chat_llm


class _FakeLLM:
    """Stand-in for ChatGoogleGenerativeAI: records ctor kwargs, offers a
    trivial `with_structured_output`."""

    instances: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeLLM.instances.append(kwargs)

    def with_structured_output(self, schema):
        return ("structured", schema)


@pytest.fixture
def spy_llm(monkeypatch):
    _FakeLLM.instances = []
    monkeypatch.setattr(llm_mod, "ChatGoogleGenerativeAI", _FakeLLM)
    return _FakeLLM


# --- config defaults ------------------------------------------------------

def test_timeout_and_retry_config_defaults_are_conservative():
    # 900 s default >> the ~426 s legitimate LLD generation observed in Phase 11
    # (it exists to bound a *hung* connection, previously unbounded).
    assert settings.gemini_timeout_seconds == 900
    assert settings.gemini_timeout_seconds >= 600

    # small SDK retry budget so it does not multiply with the structured
    # agents' own 3-attempt application loop (was up to 6 x 3 = 18).
    assert settings.gemini_max_retries == 2
    assert 0 <= settings.gemini_max_retries <= 3


def test_timeout_and_retry_are_env_configurable(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "1800")
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "0")
    fresh = Settings()
    assert fresh.gemini_timeout_seconds == 1800
    assert fresh.gemini_max_retries == 0
    # model / temperature untouched by this phase
    assert fresh.gemini_model == settings.gemini_model
    assert fresh.gemini_temperature == settings.gemini_temperature


# --- build_chat_llm passes the policy explicitly ------------------------

def test_build_chat_llm_passes_explicit_timeout_and_retries(spy_llm):
    build_chat_llm()
    assert len(spy_llm.instances) == 1
    kw = spy_llm.instances[0]
    assert kw["timeout"] == settings.gemini_timeout_seconds
    assert kw["max_retries"] == settings.gemini_max_retries
    # unchanged config, still sourced from settings
    assert kw["model"] == settings.gemini_model
    assert kw["temperature"] == settings.gemini_temperature
    assert kw["google_api_key"] == settings.google_api_key


def test_build_chat_llm_allows_a_per_call_max_retries_override(spy_llm):
    build_chat_llm(max_retries=0)
    assert spy_llm.instances[0]["max_retries"] == 0
    # still the configured timeout
    assert spy_llm.instances[0]["timeout"] == settings.gemini_timeout_seconds


def test_build_chat_llm_reads_settings_at_call_time(spy_llm, monkeypatch):
    monkeypatch.setattr(settings, "gemini_timeout_seconds", 4242)
    monkeypatch.setattr(settings, "gemini_max_retries", 5)
    build_chat_llm()
    assert spy_llm.instances[0]["timeout"] == 4242
    assert spy_llm.instances[0]["max_retries"] == 5


# --- lazy construction: agents ----------------------------------------

_PLAIN_AGENTS = (
    ("app.agents.business_analyst.agent", "BusinessAnalystAgent"),
    ("app.agents.solution_architect.agent", "SolutionArchitectAgent"),
    ("app.agents.initial_user_story.agent", "InitialUserStoryAgent"),
    ("app.agents.low_level_design.agent", "LowLevelDesignAgent"),
    ("app.agents.user_story_refinement.agent", "UserStoryRefinementAgent"),
)


@pytest.mark.parametrize("mod_name,cls_name", _PLAIN_AGENTS)
def test_plain_agent_builds_no_client_until_first_invoke(spy_llm, mod_name, cls_name):
    import importlib

    cls = getattr(importlib.import_module(mod_name), cls_name)
    agent = cls()
    assert agent._llm is None
    assert spy_llm.instances == []          # construction built nothing

    built = agent._ensure_llm()
    assert isinstance(built, _FakeLLM)
    assert len(spy_llm.instances) == 1
    # cached — a second call does not rebuild
    assert agent._ensure_llm() is built
    assert len(spy_llm.instances) == 1


@pytest.mark.parametrize("mod_name,cls_name", [
    ("app.agents.test_case.agent", "TestCaseAgent"),
    ("app.agents.closure_report.agent", "ClosureReportAgent"),
])
def test_structured_agent_lazy_client_and_wrapper(spy_llm, mod_name, cls_name):
    import importlib

    cls = getattr(importlib.import_module(mod_name), cls_name)

    agent = cls()  # structured=True by default
    assert agent._structured is True
    assert agent._llm is None
    assert agent._structured_llm is None
    assert spy_llm.instances == []

    wrapper = agent._ensure_structured_llm()
    assert isinstance(wrapper, tuple) and wrapper[0] == "structured"
    assert len(spy_llm.instances) == 1     # exactly one base client built
    # cached
    assert agent._ensure_structured_llm() is wrapper
    assert len(spy_llm.instances) == 1

    # structured=False: no wrapper, ever
    off = cls(structured=False)
    assert off._ensure_structured_llm() is None


def test_structured_agent_honours_a_pre_injected_fake(spy_llm):
    from app.agents.test_case.agent import TestCaseAgent

    agent = TestCaseAgent(structured=True)
    sentinel = object()
    agent._structured_llm = sentinel                # inject before first use
    assert agent._ensure_structured_llm() is sentinel
    assert spy_llm.instances == []                  # nothing was built


# --- lazy construction: services & read-only entry points -----------

def test_constructing_every_service_builds_no_client(spy_llm):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.closure_report.service import ClosureReportService
    from app.agents.initial_user_story.service import InitialUserStoryService
    from app.agents.low_level_design.service import LowLevelDesignService
    from app.agents.solution_architect.service import SolutionArchitectService
    from app.agents.test_case.service import TestCaseService
    from app.agents.user_story_refinement.service import UserStoryRefinementService

    pid = "p11a_svc"
    BusinessAnalystService(project_id=pid)
    SolutionArchitectService(project_id=pid)
    InitialUserStoryService(project_id=pid)
    LowLevelDesignService(project_id=pid)
    UserStoryRefinementService(project_id=pid)
    TestCaseService(project_id=pid)
    ClosureReportService(project_id=pid)

    assert spy_llm.instances == []   # the whole read-only surface stays free


def test_status_and_reports_build_no_client(spy_llm):
    from app.orchestration.status import sdlc_status
    from app.quality.project_quality_report import build_project_reports_for_project

    pid = "p11a_readonly"
    # Both construct their own full service set internally with no injection.
    sdlc_status(pid)
    build_project_reports_for_project(pid)

    assert spy_llm.instances == []
