"""
Bounded-retry / backoff coverage for TestCaseAgent._invoke_structured
(app/agents/test_case/agent.py).

No Gemini: a fake structured LLM is injected and time.sleep is monkeypatched to a
no-op so the tests are instant and deterministic.
"""

import pytest
from pydantic import ValidationError

from app.agents.test_case.agent import (
    _RETRY_MAX_ATTEMPTS,
    TestCaseAgent,
    TestCaseAgentError,
    _is_transient_llm_error,
    _retry_backoff_seconds,
)
from app.agents.test_case.schema import TestCaseList

_GOOD_PAYLOAD = {
    "test_cases": [{
        "id": "TC-001", "title": "Do a thing",
        "requirement_or_story_ref": "FR-1",
        "test_steps": ["Open it", "Use it"],
        "expected_result": "It works",
        "priority": "High", "test_type": "Functional",
    }]
}


class _Transient503(Exception):
    """Mimics a langchain-google-genai 503 UNAVAILABLE / high-demand error."""
    def __str__(self):
        return "503 UNAVAILABLE: The model is overloaded. Please try again later. (high demand)"


class _FakeStructuredLLM:
    """Yields a scripted sequence: each item is either an Exception to raise or a
    TestCaseList to return. Counts calls."""
    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        item = self._script.pop(0) if self._script else self._script_default()
        if isinstance(item, BaseException):
            raise item
        return item

    @staticmethod
    def _script_default():
        return TestCaseList(**_GOOD_PAYLOAD)


@pytest.fixture
def agent(monkeypatch):
    """A real TestCaseAgent (no network at construction) with time.sleep no-op'd."""
    slept: list[float] = []
    monkeypatch.setattr(
        "app.agents.test_case.agent.time.sleep", lambda s: slept.append(s)
    )
    a = TestCaseAgent(structured=True)
    a._slept = slept  # test-visible record of backoff sleeps
    return a


# --- retry behaviour ----------------------------------------------------

def test_transient_failure_then_success_retries_and_succeeds(agent):
    fake = _FakeStructuredLLM([_Transient503(), TestCaseList(**_GOOD_PAYLOAD)])
    agent._structured_llm = fake

    result = agent._invoke_structured("prompt")

    assert isinstance(result, TestCaseList)
    assert result.test_cases[0].id == "TC-001"
    assert fake.calls == 2                    # one failure, one success
    assert len(agent._slept) == 1            # exactly one backoff between them
    assert agent._slept[0] > 0


def test_repeated_transient_failures_stop_after_bounded_attempts(agent):
    fake = _FakeStructuredLLM([_Transient503()] * 10)  # always fails
    agent._structured_llm = fake

    with pytest.raises(TestCaseAgentError) as ei:
        agent._invoke_structured("prompt")

    assert "503" in str(ei.value) or "overloaded" in str(ei.value).lower()
    assert fake.calls == _RETRY_MAX_ATTEMPTS           # 3, not more
    assert len(agent._slept) == _RETRY_MAX_ATTEMPTS - 1  # backoff only between attempts


def test_non_transient_error_is_not_retried(agent):
    class _BadRequest(Exception):
        def __str__(self):
            return "400 INVALID_ARGUMENT: request payload is malformed"

    fake = _FakeStructuredLLM([_BadRequest()] * 5)
    agent._structured_llm = fake

    with pytest.raises(TestCaseAgentError):
        agent._invoke_structured("prompt")

    assert fake.calls == 1                    # tried once, gave up
    assert agent._slept == []                # no backoff


def test_schema_validation_error_is_not_retried(agent):
    try:
        TestCaseList(**{"test_cases": []})    # provoke a real ValidationError
    except ValidationError as ve:
        real_validation_error = ve

    fake = _FakeStructuredLLM([real_validation_error] * 5)
    agent._structured_llm = fake

    with pytest.raises(TestCaseAgentError):
        agent._invoke_structured("prompt")

    assert fake.calls == 1
    assert agent._slept == []


def test_successful_call_invokes_model_exactly_once(agent):
    fake = _FakeStructuredLLM([TestCaseList(**_GOOD_PAYLOAD)])
    agent._structured_llm = fake

    result = agent._invoke_structured("prompt")

    assert isinstance(result, TestCaseList)
    assert fake.calls == 1
    assert agent._slept == []                # no retry, no backoff on success


def test_none_result_is_not_retried_and_still_raises(agent):
    fake = _FakeStructuredLLM([None] * 5)     # invoke returns None (no exception)
    agent._structured_llm = fake

    with pytest.raises(TestCaseAgentError, match="no structured result"):
        agent._invoke_structured("prompt")

    assert fake.calls == 1                    # returning None is not a transient failure
    assert agent._slept == []


def test_two_transient_then_success_uses_all_three_attempts(agent):
    fake = _FakeStructuredLLM([_Transient503(), _Transient503(), TestCaseList(**_GOOD_PAYLOAD)])
    agent._structured_llm = fake

    result = agent._invoke_structured("prompt")

    assert isinstance(result, TestCaseList)
    assert fake.calls == 3
    assert len(agent._slept) == 2


# --- helper unit tests ------------------------------------------------

@pytest.mark.parametrize("text", [
    "503 UNAVAILABLE: model overloaded, high demand",
    "The service is temporarily unavailable, please try again",
    "429 Too Many Requests: rate limit exceeded",
    "RESOURCE_EXHAUSTED: quota",
    "google.api_core.exceptions.ServiceUnavailable: 503 upstream connect error",
    "DEADLINE_EXCEEDED",
])
def test_is_transient_true_for_capacity_errors(text):
    assert _is_transient_llm_error(Exception(text)) is True


@pytest.mark.parametrize("text", [
    "400 INVALID_ARGUMENT: bad request",
    "401 UNAUTHENTICATED: API key invalid",
    "404 NOT_FOUND: model not found",
    "something completely unrelated",
])
def test_is_transient_false_for_client_errors(text):
    assert _is_transient_llm_error(Exception(text)) is False


def test_is_transient_false_for_validation_error():
    try:
        TestCaseList(**{"test_cases": []})
    except ValidationError as ve:
        assert _is_transient_llm_error(ve) is False


def test_is_transient_true_for_numeric_status_code_attr():
    exc = Exception("upstream error")
    exc.code = 503
    assert _is_transient_llm_error(exc) is True
    exc2 = Exception("bad")
    exc2.status_code = 400
    assert _is_transient_llm_error(exc2) is False


def test_backoff_is_bounded_and_positive():
    d1 = _retry_backoff_seconds(1)
    d2 = _retry_backoff_seconds(2)
    d9 = _retry_backoff_seconds(9)   # would be huge without the cap
    assert 0 < d1 <= 1.0
    assert 0 < d2 <= 2.0
    assert 0 < d9 <= 8.0             # capped at _RETRY_MAX_DELAY_S
