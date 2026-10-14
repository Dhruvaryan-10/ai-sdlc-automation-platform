"""
Free-tier stabilisation — Gemini 429 / 503 handling.

Two layers of evidence, both fully offline (no Gemini quota is consumed):

  * unit tests of `app.utils.gemini_retry` against REAL `google.genai` error
    objects (`ClientError` / `ServerError`) and the real LangChain wrapper
    (`ChatGoogleGenerativeAIError ... from ClientError`), built locally;
  * end-to-end tests that run a real agent through the real
    `ChatGoogleGenerativeAI` client, whose HTTP layer is an in-process
    `httpx.MockTransport` returning Google-shaped responses. These count the
    actual HTTP requests the whole stack (SDK + agent loop) would send.

`time.sleep` is monkeypatched everywhere, so nothing actually waits.
"""

from __future__ import annotations

import functools
import importlib
import logging

import httpx
import pytest
from google.genai import errors as genai_errors
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError

import app.utils.llm as llm_mod
from app.utils import gemini_retry as gr
from app.utils.config import Settings, settings
from app.utils.errors import ErrorCategory, classify

_FAKE_KEY = "AIzaSyD" + "Q" * 33


# --- Google-shaped payloads -------------------------------------------------

def _body_429(*, quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
              retry_delay="42s", retry_text="Please retry in 42.7s."):
    details = [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": quota_id,
            "quotaValue": "5",
        }],
    }]
    if retry_delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": retry_delay})
    return {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": ("You exceeded your current quota. Quota exceeded for metric: "
                    "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                    f"limit: 5, model: gemini-3.6-flash\n{retry_text}"),
        "details": details,
    }}


_BODY_503 = {"error": {
    "code": 503, "status": "UNAVAILABLE",
    "message": "This model is currently experiencing high demand. Spikes in demand "
               "are usually temporary. Please try again later.",
}}
_BODY_404_MODEL = {"error": {
    "code": 404, "status": "NOT_FOUND",
    "message": "models/gemini-9.9-flash is not found for API version v1beta, or is "
               "not supported for generateContent.",
}}
_BODY_OK = {
    "candidates": [{"content": {"role": "model", "parts": [{"text": "# BRD\nok body"}]},
                    "finishReason": "STOP", "index": 0}],
    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "totalTokenCount": 8},
}


def _client_error(body, *, headers=None):
    """A real google.genai ClientError, wrapped exactly like LangChain does."""
    resp = httpx.Response(body["error"]["code"], json=body, headers=headers or {})
    root = genai_errors.ClientError(body["error"]["code"], body, resp)
    try:
        raise ChatGoogleGenerativeAIError(
            f"Error calling model 'gemini-3.6-flash' ({root.status}): {root}"
        ) from root
    except ChatGoogleGenerativeAIError as wrapped:
        return wrapped


def _server_error(body=_BODY_503, *, headers=None):
    resp = httpx.Response(body["error"]["code"], json=body, headers=headers or {})
    return genai_errors.ServerError(body["error"]["code"], body, resp)


# --- 1. configured model ----------------------------------------------------

def test_configured_model_is_loaded_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "  gemini-3.6-flash  ")
    fresh = Settings(_env_file=None, google_api_key=_FAKE_KEY)
    assert fresh.gemini_model == "gemini-3.6-flash"      # trimmed


def test_default_model_matches_the_verified_identifier():
    assert Settings(_env_file=None, google_api_key=_FAKE_KEY).gemini_model == "gemini-3.6-flash"


@pytest.mark.parametrize("bad", ["", "   ", "gemini 3.6 flash"])
def test_invalid_model_name_fails_fast(monkeypatch, bad):
    from pydantic import ValidationError

    monkeypatch.setenv("GEMINI_MODEL", bad)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, google_api_key=_FAKE_KEY)


def test_every_agent_client_uses_the_configured_model(monkeypatch):
    seen = []

    class _Spy:
        def __init__(self, **kw):
            seen.append(kw["model"])

    monkeypatch.setattr(llm_mod, "ChatGoogleGenerativeAI", _Spy)
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.6-flash")
    for mod, cls in (
        ("app.agents.business_analyst.agent", "BusinessAnalystAgent"),
        ("app.agents.solution_architect.agent", "SolutionArchitectAgent"),
        ("app.agents.initial_user_story.agent", "InitialUserStoryAgent"),
        ("app.agents.low_level_design.agent", "LowLevelDesignAgent"),
        ("app.agents.user_story_refinement.agent", "UserStoryRefinementAgent"),
        ("app.agents.test_case.agent", "TestCaseAgent"),
        ("app.agents.closure_report.agent", "ClosureReportAgent"),
    ):
        getattr(importlib.import_module(mod), cls)()._ensure_llm()
    assert seen == ["gemini-3.6-flash"] * 7


def test_no_stale_hardcoded_model_names_in_app_code():
    from pathlib import Path

    root = Path(llm_mod.__file__).resolve().parents[1]
    offenders = [
        str(p) for p in root.rglob("*.py")
        if "gemini-3.5" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --- retry-delay extraction ------------------------------------------------------

def test_structured_retry_info_is_preferred():
    exc = _client_error(_body_429(retry_delay="42s", retry_text="Please retry in 99s."),
                        headers={"Retry-After": "7"})
    assert gr.extract_retry_delay_seconds(exc) == 42.0


def test_retry_after_header_used_when_no_structured_delay():
    exc = _client_error(_body_429(retry_delay=None, retry_text=""), headers={"Retry-After": "17"})
    assert gr.extract_retry_delay_seconds(exc) == 17.0


def test_retry_delay_parsed_from_message_text():
    exc = _client_error(_body_429(retry_delay=None, retry_text="Please retry in 48.3s."))
    assert gr.extract_retry_delay_seconds(exc) == pytest.approx(48.3)


@pytest.mark.parametrize("text,expected", [
    ("retry_delay { seconds: 45 }", 45.0),
    ("'retryDelay': '49s'", 49.0),
    ("please retry after 1500ms", 1.5),
    ("nothing useful here", None),
    ("retry in -5s", None),
])
def test_retry_delay_text_variants(text, expected):
    assert gr.extract_retry_delay_seconds(RuntimeError(text)) == expected


# --- classification ----------------------------------------------------------

def test_per_minute_429_is_a_rate_limit_with_delay():
    f = gr.classify_gemini_failure(_client_error(_body_429()))
    assert f.kind is gr.GeminiFailureKind.RATE_LIMIT
    assert f.status_code == 429
    assert f.retry_after_seconds == 42.0


def test_daily_429_is_daily_quota_even_with_a_short_retry_delay():
    exc = _client_error(_body_429(quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                  retry_delay="20s"))
    assert gr.classify_gemini_failure(exc).kind is gr.GeminiFailureKind.DAILY_QUOTA


def test_429_without_window_information_is_generic_quota():
    exc = _client_error({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                   "message": "Resource has been exhausted."}})
    assert gr.classify_gemini_failure(exc).kind is gr.GeminiFailureKind.QUOTA


def test_503_server_error_is_unavailable():
    f = gr.classify_gemini_failure(_server_error())
    assert f.kind is gr.GeminiFailureKind.UNAVAILABLE and f.status_code == 503


def test_unknown_model_is_a_configuration_error():
    exc = _client_error(_BODY_404_MODEL)
    assert gr.classify_gemini_failure(exc).kind is gr.GeminiFailureKind.MODEL_CONFIG


def test_describe_is_bounded_and_never_contains_provider_text_or_secrets():
    body = _body_429()
    body["error"]["message"] += f" key={_FAKE_KEY}"
    line = gr.describe(_client_error(body))
    assert "kind=rate_limit" in line and "status=429" in line and "retry_after=42.0s" in line
    assert _FAKE_KEY not in line and "quota" not in line.lower() and len(line) < 160


# --- user-facing messages ------------------------------------------------------

def _wrapped_in_agent_error(exc):
    from app.agents.business_analyst.agent import BusinessAnalystAgentError

    try:
        raise BusinessAnalystAgentError(f"Gemini API call failed: {exc}") from exc
    except BusinessAnalystAgentError as outer:
        return outer


def test_rate_limit_message_tells_the_user_how_long_to_wait():
    res = classify(_wrapped_in_agent_error(_client_error(_body_429(retry_delay="42s"))))
    assert res.category is ErrorCategory.LLM_RATE_LIMIT
    assert res.retry_after_seconds == 42.0
    assert res.user_message.startswith("Gemini's free-tier request limit has been reached.")
    assert "42 seconds" in res.user_message


def test_daily_quota_produces_a_clear_message():
    exc = _client_error(_body_429(quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
    res = classify(_wrapped_in_agent_error(exc))
    assert res.category is ErrorCategory.LLM_DAILY_QUOTA
    assert res.retryable is False
    assert "daily quota appears to be exhausted" in res.user_message
    assert "usage dashboard" in res.user_message


def test_uncertain_quota_gets_the_generic_message():
    exc = _client_error({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                   "message": "Resource has been exhausted."}})
    assert classify(_wrapped_in_agent_error(exc)).category is ErrorCategory.LLM_QUOTA


def test_503_message_is_temporarily_unavailable():
    res = classify(_wrapped_in_agent_error(_server_error()))
    assert res.category is ErrorCategory.LLM_UNAVAILABLE
    assert res.user_message == "Gemini is temporarily unavailable. Please try again later."


def test_model_configuration_message():
    res = classify(_wrapped_in_agent_error(_client_error(_BODY_404_MODEL)))
    assert res.category is ErrorCategory.CONFIG_INVALID
    assert res.user_message == (
        "Gemini configuration is invalid. Check the deployment configuration and "
        "application logs."
    )


def test_user_messages_never_contain_the_api_key():
    body = _body_429()
    body["error"]["message"] += f" key={_FAKE_KEY}"
    assert _FAKE_KEY not in classify(_wrapped_in_agent_error(_client_error(body))).user_message


# --- policy unit tests --------------------------------------------------------------

def _policy(**kw):
    defaults = dict(max_attempts=3, is_transient=lambda e: True,
                    backoff=lambda a: 1.0 * a, max_wait_seconds=60,
                    rate_limit_jitter=lambda: 0.5)
    defaults.update(kw)
    return gr.GeminiRetryPolicy(**defaults)


def test_policy_waits_for_googles_delay_once_then_stops():
    p = _policy()
    exc = _client_error(_body_429(retry_delay="42s"))
    assert p.next_delay(exc, 1) == pytest.approx(42.5)   # delay + jitter, never less
    assert p.next_delay(exc, 2) is None                   # a second 429: give up
    assert p.rate_limit_retries == 1


def test_policy_does_not_block_when_the_delay_exceeds_the_budget():
    p = _policy(max_wait_seconds=30)
    assert p.next_delay(_client_error(_body_429(retry_delay="42s")), 1) is None
    assert p.waited_seconds == 0


def test_policy_zero_budget_means_fail_fast():
    p = _policy(max_wait_seconds=0)
    assert p.next_delay(_client_error(_body_429(retry_delay="1s")), 1) is None
    assert p.next_delay(_server_error(), 1) is None


def test_policy_never_retries_daily_quota_or_unknown_quota_or_config():
    p = _policy()
    daily = _client_error(_body_429(quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
    unknown = _client_error({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                       "message": "Resource has been exhausted."}})
    assert p.next_delay(daily, 1) is None
    assert p.next_delay(unknown, 1) is None
    assert p.next_delay(_client_error(_BODY_404_MODEL), 1) is None


def test_policy_503_uses_backoff_raised_to_a_server_delay():
    p = _policy(backoff=lambda a: 0.5)
    assert p.next_delay(_server_error(headers={"Retry-After": "5"}), 1) == 5.0
    assert p.next_delay(_server_error(), 2) == 0.5
    assert p.next_delay(_server_error(), 3) is None       # attempt budget reached


def test_policy_total_wait_is_bounded():
    p = _policy(backoff=lambda a: 25.0, max_attempts=5, max_wait_seconds=60)
    assert p.next_delay(_server_error(), 1) == 25.0
    assert p.next_delay(_server_error(), 2) == 25.0
    assert p.next_delay(_server_error(), 3) is None       # 75 s would exceed 60 s
    assert p.waited_seconds == 50.0


def test_policy_respects_the_agents_non_transient_rule():
    p = _policy(is_transient=lambda e: False)
    assert p.next_delay(RuntimeError("400 INVALID_ARGUMENT"), 1) is None


# --- end-to-end: real agent + real LangChain/SDK client + fake HTTP transport ---------

class _Transport:
    """Scripted in-process Gemini endpoint. Counts real HTTP requests."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        status, body, headers = self.script.pop(0) if self.script else (200, _BODY_OK, {})
        return httpx.Response(status, json=body, headers=headers)


@pytest.fixture
def e2e(monkeypatch):
    """Return a factory: script -> (agent, transport, slept). Uses the REAL
    build_chat_llm() + ChatGoogleGenerativeAI with the configured model and SDK
    retry budget; only the HTTP transport is fake."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    import app.agents.business_analyst.agent as ba

    def factory(script):
        transport = _Transport(script)
        monkeypatch.setattr(
            llm_mod, "ChatGoogleGenerativeAI",
            functools.partial(ChatGoogleGenerativeAI,
                              client_args={"transport": httpx.MockTransport(transport)}),
        )
        slept: list[float] = []
        monkeypatch.setattr(ba.time, "sleep", lambda s: slept.append(s))
        return ba.BusinessAnalystAgent(), transport, slept

    return factory


def test_e2e_successful_response_passes_through(e2e):
    agent, transport, slept = e2e([(200, _BODY_OK, {})])
    assert agent._invoke("prompt") == "# BRD\nok body"
    assert transport.requests == 1 and slept == []


def test_e2e_429_with_long_delay_sends_exactly_one_request(e2e):
    """The old stack sent 6 requests (SDK 2 x agent 3) in ~5 s for one 429."""
    from app.agents.business_analyst.agent import BusinessAnalystAgentError

    agent, transport, slept = e2e([(429, _body_429(retry_delay="120s"), {})] * 10)
    with pytest.raises(BusinessAnalystAgentError) as ei:
        agent._invoke("prompt")
    assert transport.requests == 1       # no SDK retry, no app retry
    assert slept == []                   # did not block the Streamlit request
    res = classify(ei.value)
    assert res.category is ErrorCategory.LLM_RATE_LIMIT and "120 seconds" in res.user_message


def test_e2e_429_waits_for_the_requested_delay_then_succeeds(e2e):
    agent, transport, slept = e2e([(429, _body_429(retry_delay="42s"), {}), (200, _BODY_OK, {})])
    assert agent._invoke("prompt") == "# BRD\nok body"
    assert transport.requests == 2
    assert len(slept) == 1 and 42.0 <= slept[0] <= 44.0   # never shorter than Google asked


def test_e2e_retry_after_header_is_respected(e2e):
    agent, transport, slept = e2e([
        (429, _body_429(retry_delay=None, retry_text=""), {"Retry-After": "30"}),
        (200, _BODY_OK, {}),
    ])
    agent._invoke("prompt")
    assert transport.requests == 2 and 30.0 <= slept[0] <= 32.0


def test_e2e_repeated_429_does_not_hammer_the_api(e2e):
    from app.agents.business_analyst.agent import BusinessAnalystAgentError

    agent, transport, slept = e2e([(429, _body_429(retry_delay="40s"), {})] * 10)
    with pytest.raises(BusinessAnalystAgentError):
        agent._invoke("prompt")
    assert transport.requests == 2       # one server-paced retry, then stop
    assert len(slept) == 1 and slept[0] >= 40.0


def test_e2e_daily_quota_is_never_retried(e2e):
    from app.agents.business_analyst.agent import BusinessAnalystAgentError

    body = _body_429(quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier", retry_delay="10s")
    agent, transport, slept = e2e([(429, body, {})] * 5)
    with pytest.raises(BusinessAnalystAgentError) as ei:
        agent._invoke("prompt")
    assert transport.requests == 1 and slept == []
    assert classify(ei.value).category is ErrorCategory.LLM_DAILY_QUOTA


def test_e2e_503_uses_bounded_exponential_backoff(e2e):
    from app.agents.business_analyst.agent import BusinessAnalystAgentError

    agent, transport, slept = e2e([(503, _BODY_503, {})] * 10)
    with pytest.raises(BusinessAnalystAgentError) as ei:
        agent._invoke("prompt")
    assert transport.requests == settings.gemini_retry_max_attempts == 3
    assert len(slept) == 2
    assert 0.5 <= slept[0] <= 1.0 and 1.0 <= slept[1] <= 2.0   # exponential, jittered
    assert classify(ei.value).category is ErrorCategory.LLM_UNAVAILABLE


def test_e2e_503_then_success(e2e):
    agent, transport, slept = e2e([(503, _BODY_503, {}), (200, _BODY_OK, {})])
    assert agent._invoke("prompt") == "# BRD\nok body"
    assert transport.requests == 2 and len(slept) == 1


def test_e2e_unknown_model_fails_once_with_config_message(e2e):
    from app.agents.business_analyst.agent import BusinessAnalystAgentError

    agent, transport, slept = e2e([(404, _BODY_404_MODEL, {})] * 3)
    with pytest.raises(BusinessAnalystAgentError) as ei:
        agent._invoke("prompt")
    assert transport.requests == 1 and slept == []
    assert classify(ei.value).user_message.startswith("Gemini configuration is invalid.")


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_e2e_logs_are_sanitized(e2e):
    from app.agents.business_analyst.agent import BusinessAnalystAgentError

    body = _body_429(retry_delay="120s")
    body["error"]["message"] += f" key={_FAKE_KEY}"
    agent, _transport, _slept = e2e([(429, body, {})])
    handler = _ListHandler()
    # app loggers do not propagate to the root logger — attach directly.
    names = ("app.agents.business_analyst.agent", "app.telemetry")
    for name in names:
        logging.getLogger(name).addHandler(handler)
    try:
        with pytest.raises(BusinessAnalystAgentError):
            agent._invoke("SECRET SOW CONTENT")
    finally:
        for name in names:
            logging.getLogger(name).removeHandler(handler)
    text = "\n".join(handler.lines)
    assert _FAKE_KEY not in text
    assert "SECRET SOW CONTENT" not in text
    assert "kind=rate_limit" in text


def test_sdk_layer_makes_a_single_request_per_agent_attempt_by_default():
    assert settings.gemini_max_retries == 1
