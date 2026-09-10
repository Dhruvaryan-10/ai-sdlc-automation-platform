"""
Phase 16 - bounded transient-retry / backoff coverage for the five remaining
generation agents:

    BRD                  - app/agents/business_analyst/agent.py
    HLD                  - app/agents/solution_architect/agent.py
    Initial User Story   - app/agents/initial_user_story/agent.py
    LLD                  - app/agents/low_level_design/agent.py
    User Story Refinement- app/agents/user_story_refinement/agent.py

The retry policy is replicated verbatim from ``app/agents/test_case/agent.py``;
these tests mirror ``tests/test_test_case_retry.py``. No Gemini: a fake LLM is
injected via ``agent._llm`` and ``time.sleep`` is monkeypatched to a no-op so the
tests are instant and deterministic.
"""

import importlib

import pytest

# (module, agent class, agent-specific error class) for each of the five agents.
_AGENTS = [
    ("app.agents.business_analyst.agent",
     "BusinessAnalystAgent", "BusinessAnalystAgentError"),
    ("app.agents.solution_architect.agent",
     "SolutionArchitectAgent", "SolutionArchitectAgentError"),
    ("app.agents.initial_user_story.agent",
     "InitialUserStoryAgent", "InitialUserStoryAgentError"),
    ("app.agents.low_level_design.agent",
     "LowLevelDesignAgent", "LLDAgentError"),
    ("app.agents.user_story_refinement.agent",
     "UserStoryRefinementAgent", "UserStoryRefinementAgentError"),
]
_IDS = [m.split(".")[-2] for m, _, _ in _AGENTS]


class _Transient503(Exception):
    """Mimics a langchain-google-genai 503 UNAVAILABLE / high-demand error."""

    def __str__(self):
        return ("503 UNAVAILABLE: The model is overloaded. "
                "Please try again later. (high demand)")


class _BadRequest(Exception):
    """A non-transient client error - must never be retried."""

    def __str__(self):
        return "400 INVALID_ARGUMENT: request payload is malformed"


class _Resp:
    """Minimal stand-in for a LangChain message (only ``.content`` is read)."""

    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """Scripted LLM: each item is an Exception to raise or a str to return as
    message content. Counts calls."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        item = self._script.pop(0) if self._script else "default body"
        if isinstance(item, BaseException):
            raise item
        return _Resp(item)


@pytest.fixture(params=_AGENTS, ids=_IDS)
def agent_ctx(request, monkeypatch):
    """Yield ``(module, agent, error_cls, slept_list)`` for one agent, with
    ``time.sleep`` no-op'd and the backoff durations recorded in ``slept``."""
    mod_name, cls_name, err_name = request.param
    mod = importlib.import_module(mod_name)
    slept: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
    agent = getattr(mod, cls_name)()
    return mod, agent, getattr(mod, err_name), slept


# --- (a) transient failure -> retry -> success --------------------------------

def test_transient_failure_then_success(agent_ctx):
    mod, agent, _err, slept = agent_ctx
    fake = _FakeLLM([_Transient503(), "good body"])
    agent._llm = fake

    out = agent._invoke("prompt")

    assert out == "good body"
    assert fake.calls == 2                 # one failure, one success
    assert len(slept) == 1                 # exactly one backoff between them
    assert slept[0] > 0


def test_two_transient_then_success_uses_all_three_attempts(agent_ctx):
    mod, agent, _err, slept = agent_ctx
    fake = _FakeLLM([_Transient503(), _Transient503(), "body v3"])
    agent._llm = fake

    out = agent._invoke("prompt")

    assert out == "body v3"
    assert fake.calls == 3
    assert len(slept) == 2


# --- (b) transient failure -> bounded retry exhaustion ----------------------

def test_repeated_transient_failures_stop_after_bounded_attempts(agent_ctx):
    mod, agent, err, slept = agent_ctx
    fake = _FakeLLM([_Transient503()] * 10)          # always fails
    agent._llm = fake

    with pytest.raises(err) as ei:
        agent._invoke("prompt")

    assert "503" in str(ei.value) or "overloaded" in str(ei.value).lower()
    assert fake.calls == mod._RETRY_MAX_ATTEMPTS               # 3, not more
    assert len(slept) == mod._RETRY_MAX_ATTEMPTS - 1           # backoff only between


# --- (c) non-transient failure -> no retry ---------------------------------

def test_non_transient_error_is_not_retried(agent_ctx):
    mod, agent, err, slept = agent_ctx
    fake = _FakeLLM([_BadRequest()] * 5)
    agent._llm = fake

    with pytest.raises(err):
        agent._invoke("prompt")

    assert fake.calls == 1                 # tried once, gave up
    assert slept == []                     # no backoff


def test_validation_error_is_not_transient(agent_ctx):
    mod, _agent, _err, _slept = agent_ctx
    from pydantic import BaseModel, ValidationError

    class _M(BaseModel):
        x: int

    try:
        _M(x="not-an-int")
    except ValidationError as ve:
        assert mod._is_transient_llm_error(ve) is False


def test_empty_response_is_not_retried_and_still_raises(agent_ctx):
    mod, agent, err, slept = agent_ctx
    fake = _FakeLLM([""] * 5)              # invoke succeeds but content is empty
    agent._llm = fake

    with pytest.raises(err, match="empty response"):
        agent._invoke("prompt")

    assert fake.calls == 1                 # an empty body is not a transient failure
    assert slept == []


# --- (d) attempt telemetry remains correct --------------------------------

def test_attempt_number_is_forwarded_to_telemetry_on_each_invocation(
    agent_ctx, monkeypatch
):
    mod, agent, _err, _slept = agent_ctx
    seen: list[tuple[str, int]] = []

    def _spy(llm, prompt, *, stage, attempt=1):
        seen.append((stage, attempt))
        return llm.invoke(prompt)

    monkeypatch.setattr(mod, "instrumented_invoke", _spy)
    agent._llm = _FakeLLM([_Transient503(), _Transient503(), "ok body"])

    out = agent._invoke("prompt")

    assert out == "ok body"
    assert [a for _s, a in seen] == [1, 2, 3]                  # monotonic per attempt
    assert {s for s, _a in seen} == {agent._TELEMETRY_STAGE}   # stage unchanged


def test_successful_call_reports_attempt_one(agent_ctx, monkeypatch):
    mod, agent, _err, _slept = agent_ctx
    seen: list[int] = []

    def _spy(llm, prompt, *, stage, attempt=1):
        seen.append(attempt)
        return llm.invoke(prompt)

    monkeypatch.setattr(mod, "instrumented_invoke", _spy)
    agent._llm = _FakeLLM(["body"])

    agent._invoke("prompt")

    assert seen == [1]


# --- (e) successful existing path remains unchanged -----------------------

def test_successful_call_invokes_model_exactly_once(agent_ctx):
    mod, agent, _err, slept = agent_ctx
    fake = _FakeLLM(["the body"])
    agent._llm = fake

    out = agent._invoke("prompt")

    assert out == "the body"
    assert fake.calls == 1
    assert slept == []                     # no retry, no backoff on success


# --- backoff-helper parity (same bounds as Test Case) ---------------------

def test_backoff_is_bounded_and_positive(agent_ctx):
    mod, _agent, _err, _slept = agent_ctx
    assert 0 < mod._retry_backoff_seconds(1) <= mod._RETRY_BASE_DELAY_S
    assert 0 < mod._retry_backoff_seconds(2) <= 2.0
    assert 0 < mod._retry_backoff_seconds(9) <= mod._RETRY_MAX_DELAY_S  # capped


def test_retry_limits_match_the_test_case_agent_policy(agent_ctx):
    mod, _agent, _err, _slept = agent_ctx
    from app.agents.test_case import agent as tc

    assert mod._RETRY_MAX_ATTEMPTS == tc._RETRY_MAX_ATTEMPTS
    assert mod._RETRY_BASE_DELAY_S == tc._RETRY_BASE_DELAY_S
    assert mod._RETRY_MAX_DELAY_S == tc._RETRY_MAX_DELAY_S
    assert mod._RETRYABLE_STATUS_CODES == tc._RETRYABLE_STATUS_CODES
    assert mod._RETRYABLE_GRPC_NAMES == tc._RETRYABLE_GRPC_NAMES
    assert mod._TRANSIENT_MARKERS == tc._TRANSIENT_MARKERS
