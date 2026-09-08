"""
Phase 12B — centralized error taxonomy (`app/utils/errors.py`).

Pure classification tests: no Gemini, no network, no persistence writes. Every
case builds a synthetic exception and asserts the `classify()` verdict, the
stable machine code, the advisory `retryable` flag, the log level, safe
user-facing wording, and — critically — that no secret / model-output / raw
library text can reach the user message.
"""

import logging
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from app.agents.business_analyst.agent import BusinessAnalystAgentError
from app.agents.business_analyst.prompt_manager import PromptRenderError
from app.agents.business_analyst.service import (
    BRDLockedError,
    EmptyDocumentError,
    UnsupportedFileTypeError,
)
from app.agents.closure_report.agent import ClosureReportAgentError
from app.agents.closure_report.service import (
    ClosureReportLockedError,
    InvalidClosureNarrativeError,
)
from app.agents.closure_report.service import NoFinalBRDError as NoFinalBRDError_CR
from app.agents.initial_user_story.service import (
    NoFinalBRDError as NoFinalBRDError_US,
    UserStoryLockedError,
)
from app.agents.low_level_design.service import LLDLockedError, NoFinalHLDError
from app.agents.solution_architect.agent import SolutionArchitectAgentError
from app.agents.solution_architect.service import (
    HLDLockedError,
    NoFinalBRDError as NoFinalBRDError_SA,
)
from app.agents.test_case.agent import TestCaseAgentError
from app.agents.test_case.service import (
    InvalidTestCaseJSONError,
    NoFinalBRDError as NoFinalBRDError_TC,
    TestCaseLockedError,
)
from app.agents.user_story_refinement.service import (
    NoFinalBRDError as NoFinalBRDError_USR,
    NoInitialUserStoriesError,
    RefinementLockedError,
)
from app.services.version_service import VersionPersistenceError
from app.utils import errors as errmod
from app.utils.errors import AppError, ErrorCategory, classify, log_app_error


# --- synthetic provider-shaped exceptions ---------------------------------

class _FakeReadTimeout(Exception):
    """Name mimics httpx.ReadTimeout for the type-name heuristic."""


class _FakeRemoteProtocolError(Exception):
    """Name mimics httpx.RemoteProtocolError."""


class _FakeClientError(Exception):
    """Name mimics google.genai.errors.ClientError; carries a status code."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _wrap(agent_exc_type, message, cause=None):
    exc = agent_exc_type(message)
    if cause is not None:
        exc.__cause__ = cause
    return exc


def _validation_error(value="MODEL_OUTPUT_LEAK_MARKER_9F3A"):
    class _M(BaseModel):
        x: int

    try:
        _M(x=value)
    except ValidationError as ve:
        return ve
    raise AssertionError("expected a ValidationError")  # pragma: no cover


# --- 1. every category is reachable + 2. every stable code ----------------

_ALL_CODES = {
    "persistence.corrupt", "persistence.io",
    "llm.quota", "llm.timeout", "llm.network", "llm.auth",
    "llm.empty_response", "llm.schema",
    "config.invalid",
    "state.missing_prerequisite", "state.locked", "state.invalid",
    "input.file",
    "unexpected",
}


def test_error_category_enum_covers_exactly_the_spec_codes():
    assert {c.value for c in ErrorCategory} == _ALL_CODES


def test_every_category_has_a_spec_row():
    for cat in ErrorCategory:
        assert cat in errmod._SPEC
        level, retryable, msg = errmod._SPEC[cat]
        assert isinstance(level, int) and isinstance(retryable, bool)
        assert isinstance(msg, str) and msg.strip()


_REPRESENTATIVE = {
    "persistence.corrupt": VersionPersistenceError(
        "Version history for 'p/hld' is corrupt and could not be recovered from a backup."
    ),
    "persistence.io": PermissionError("[Errno 13] Permission denied: outputs/p/versions.json"),
    "llm.quota": _wrap(TestCaseAgentError, "Gemini API call failed: 429 RESOURCE_EXHAUSTED — quota exceeded"),
    "llm.timeout": _wrap(SolutionArchitectAgentError, "Gemini API call failed: request timed out after 900s"),
    "llm.network": _wrap(BusinessAnalystAgentError, "Gemini API call failed: 503 UNAVAILABLE — the model is overloaded"),
    "llm.auth": _wrap(TestCaseAgentError, "Gemini API call failed: API key not valid. Pass a valid API key."),
    "llm.empty_response": TestCaseAgentError("Gemini returned an empty response"),
    "llm.schema": InvalidTestCaseJSONError("The QA agent did not return valid JSON: Expecting ','"),
    "config.invalid": PromptRenderError("Prompt template references placeholder '{missing}' which was not supplied."),
    "state.missing_prerequisite": NoFinalBRDError_SA("Accept a BRD before generating the HLD."),
    "state.locked": BRDLockedError("The final BRD is locked. Unlock it before making further changes."),
    "state.invalid": ValueError("No existing BRD version to refine. Generate an initial BRD first."),
    "input.file": UnsupportedFileTypeError("nope"),
    "unexpected": KeyError("totally unexpected"),
}


@pytest.mark.parametrize("code", sorted(_ALL_CODES))
def test_representative_exception_maps_to_its_code(code):
    result = classify(_REPRESENTATIVE[code])
    assert isinstance(result, AppError)
    assert result.code == code
    assert result.category.value == code
    assert result.user_message.strip()


# --- 3. retryable metadata + 4. log level metadata ----------------------

_RETRYABLE_CODES = {"llm.timeout", "llm.network", "llm.empty_response", "llm.schema"}
_LEVEL_BY_CODE = {
    "state.missing_prerequisite": logging.INFO,
    "state.locked": logging.INFO,
    "state.invalid": logging.INFO,
    "input.file": logging.INFO,
    "llm.quota": logging.WARNING,
    "llm.timeout": logging.WARNING,
    "llm.network": logging.WARNING,
    "llm.empty_response": logging.WARNING,
    "llm.schema": logging.WARNING,
    "llm.auth": logging.ERROR,
    "config.invalid": logging.ERROR,
    "persistence.io": logging.ERROR,
    "unexpected": logging.ERROR,
    "persistence.corrupt": logging.CRITICAL,
}


@pytest.mark.parametrize("code", sorted(_ALL_CODES))
def test_retryable_metadata_matches_spec(code):
    assert classify(_REPRESENTATIVE[code]).retryable is (code in _RETRYABLE_CODES)


@pytest.mark.parametrize("code", sorted(_ALL_CODES))
def test_log_level_metadata_matches_spec(code):
    assert classify(_REPRESENTATIVE[code]).log_level == _LEVEL_BY_CODE[code]


# --- 5. wrapped __cause__ detection ------------------------------------

def test_cause_chain_detects_provider_beyond_the_wrapper_type():
    ve = _validation_error()
    wrapped = _wrap(TestCaseAgentError, "Gemini structured call failed: <schema errors>", cause=ve)
    assert classify(wrapped).category is ErrorCategory.LLM_SCHEMA


def test_cause_chain_detects_quota_via_status_code_on_the_cause():
    cause = _FakeClientError("upstream boom", code=429)
    wrapped = _wrap(BusinessAnalystAgentError, "Gemini API call failed: upstream boom", cause=cause)
    assert classify(wrapped).category is ErrorCategory.LLM_QUOTA


def test_cause_chain_detects_timeout_via_cause_type_name():
    cause = _FakeReadTimeout("read op timed out")
    wrapped = _wrap(SolutionArchitectAgentError, "Gemini API call failed", cause=cause)
    assert classify(wrapped).category is ErrorCategory.LLM_TIMEOUT


def test_cause_chain_detects_network_via_cause_type_name():
    cause = _FakeRemoteProtocolError("server disconnected without sending a complete response")
    wrapped = _wrap(ClosureReportAgentError, "Gemini API call failed", cause=cause)
    assert classify(wrapped).category is ErrorCategory.LLM_NETWORK


# --- 6. VersionPersistenceError ---------------------------------------

def test_version_persistence_error_is_persistence_corrupt_not_generic_runtime():
    vpe = VersionPersistenceError("Version history for 'abc123' is corrupt and could not be recovered.")
    res = classify(vpe)
    assert res.category is ErrorCategory.PERSISTENCE_CORRUPT
    assert res.code == "persistence.corrupt"
    assert res.retryable is False
    assert res.log_level == logging.CRITICAL
    # non-retryable wording: does NOT tell the user to just "try again"
    low = res.user_message.lower()
    assert "restore" in low or "backup" in low
    assert res.user_message != errmod.UNEXPECTED_MESSAGE


def test_version_persistence_error_wins_even_with_a_validation_error_cause():
    vpe = VersionPersistenceError("corrupt")
    vpe.__cause__ = _validation_error()  # a schema-shaped cause must NOT win
    assert classify(vpe).category is ErrorCategory.PERSISTENCE_CORRUPT


# --- 7. quota 429 -----------------------------------------------------

@pytest.mark.parametrize("msg", [
    "Gemini API call failed: 429 RESOURCE_EXHAUSTED",
    "Gemini API call failed: quota exceeded for generate_content_free_tier_requests",
    "Gemini API call failed: rate limit reached, retry in 30s",
])
def test_quota_variants(msg):
    assert classify(_wrap(TestCaseAgentError, msg)).category is ErrorCategory.LLM_QUOTA


def test_quota_is_advisory_non_retryable():
    assert classify(_wrap(TestCaseAgentError, "429 RESOURCE_EXHAUSTED")).retryable is False


# --- 8. timeout -----------------------------------------------------

@pytest.mark.parametrize("msg", [
    "Gemini API call failed: deadline exceeded",
    "Gemini API call failed: the read operation timed out",
    "Gemini API call failed: httpx.ReadTimeout",
])
def test_timeout_variants(msg):
    assert classify(_wrap(SolutionArchitectAgentError, msg)).category is ErrorCategory.LLM_TIMEOUT


# --- 9. network / transport --------------------------------------

@pytest.mark.parametrize("msg", [
    "Gemini API call failed: 503 Service Unavailable",
    "Gemini API call failed: the model is overloaded, please try again later",
    "Gemini API call failed: Server disconnected without sending a response",
    "Gemini API call failed: Connection reset by peer",
])
def test_network_variants(msg):
    assert classify(_wrap(BusinessAnalystAgentError, msg)).category is ErrorCategory.LLM_NETWORK


def test_unclassifiable_wrapped_gemini_error_falls_back_to_network():
    assert classify(_wrap(TestCaseAgentError, "Gemini API call failed: something odd")).category is ErrorCategory.LLM_NETWORK


# --- 10. auth -----------------------------------------------------

@pytest.mark.parametrize("msg", [
    "Gemini API call failed: API key not valid. Pass a valid API key.",
    "Gemini API call failed: PERMISSION_DENIED",
    "Gemini API call failed: request had invalid authentication credentials",
])
def test_auth_variants(msg):
    res = classify(_wrap(ClosureReportAgentError, msg))
    assert res.category is ErrorCategory.LLM_AUTH
    assert res.retryable is False
    assert res.log_level == logging.ERROR


def test_auth_via_status_code_403():
    cause = _FakeClientError("forbidden", code=403)
    assert classify(_wrap(BusinessAnalystAgentError, "Gemini API call failed", cause=cause)).category is ErrorCategory.LLM_AUTH


# --- 11. empty / unusable response --------------------------------

@pytest.mark.parametrize("msg", [
    "Gemini returned an empty response",
    "Gemini returned no structured result",
    "Gemini structured result contained no test cases",
])
def test_empty_response_variants(msg):
    res = classify(TestCaseAgentError(msg))
    assert res.category is ErrorCategory.LLM_EMPTY_RESPONSE
    assert res.retryable is True


# --- 12. Pydantic ValidationError -------------------------------

def test_raw_pydantic_validation_error_is_llm_schema():
    assert classify(_validation_error()).category is ErrorCategory.LLM_SCHEMA


def test_wrapped_pydantic_validation_error_is_llm_schema():
    ve = _validation_error()
    assert classify(_wrap(ClosureReportAgentError, f"Gemini structured call failed: {ve}", cause=ve)).category is ErrorCategory.LLM_SCHEMA


# --- 13/14. Invalid* service validation errors ----------------

def test_invalid_test_case_json_error_is_llm_schema():
    res = classify(InvalidTestCaseJSONError("Test case id 'TC-1' must have the form TC-000."))
    assert res.category is ErrorCategory.LLM_SCHEMA
    assert res.retryable is True


def test_invalid_closure_narrative_error_is_llm_schema():
    assert classify(InvalidClosureNarrativeError("Closure narrative is missing required field 'limitations'.")).category is ErrorCategory.LLM_SCHEMA


# --- 15. prerequisite exceptions (all 7 types, message preserved) -----

@pytest.mark.parametrize("exc", [
    NoFinalBRDError_SA("Accept a BRD before generating the HLD."),
    NoFinalBRDError_US("Accept a BRD before generating user stories."),
    NoFinalBRDError_TC("Accept a BRD before generating test cases."),
    NoFinalBRDError_CR("Accept a BRD before generating the project closure report."),
    NoFinalBRDError_USR("Accept a BRD before refining the user stories."),
    NoFinalHLDError("Accept an HLD before generating the LLD."),
    NoInitialUserStoriesError("Generate the initial user stories before refining them."),
])
def test_prerequisite_exceptions_preserve_message(exc):
    res = classify(exc)
    assert res.category is ErrorCategory.STATE_MISSING_PREREQUISITE
    assert res.code == "state.missing_prerequisite"
    assert res.user_message == str(exc)
    assert res.log_level == logging.INFO
    assert res.retryable is False


# --- 16. locked / state exceptions (all 7 types, message preserved) ---

@pytest.mark.parametrize("exc", [
    BRDLockedError("The final BRD is locked. Unlock it before making further changes."),
    HLDLockedError("The final HLD is locked."),
    UserStoryLockedError("The final user stories are locked."),
    LLDLockedError("The final LLD is locked."),
    RefinementLockedError("The refined user stories are locked."),
    TestCaseLockedError("The final test cases are locked. Unlock them before making further changes."),
    ClosureReportLockedError("The final closure report is locked. Unlock it before regenerating."),
])
def test_locked_exceptions_preserve_message(exc):
    res = classify(exc)
    assert res.category is ErrorCategory.STATE_LOCKED
    assert res.user_message == str(exc)
    assert res.log_level == logging.INFO


# --- 17. UnsupportedFileTypeError ----------------------------

def test_unsupported_file_type_error():
    res = classify(UnsupportedFileTypeError("x"))
    assert res.category is ErrorCategory.INPUT_FILE
    assert "DOCX" in res.user_message and "PDF" in res.user_message


# --- 18. EmptyDocumentError -------------------------------

def test_empty_document_error():
    res = classify(EmptyDocumentError("No extractable text found in 'sow.pdf'"))
    assert res.category is ErrorCategory.INPUT_FILE
    assert "scanned" in res.user_message.lower()
    assert "sow.pdf" not in res.user_message  # no file path echoed


# --- 19. PromptRenderError ------------------------------

def test_prompt_render_error_is_config_invalid():
    res = classify(PromptRenderError("Prompt template references placeholder '{x}' which was not supplied."))
    assert res.category is ErrorCategory.CONFIG_INVALID
    assert res.log_level == logging.ERROR
    assert res.retryable is False


# --- 20. missing-prompt FileNotFoundError -------------

def test_missing_prompt_file_is_config_invalid():
    assert classify(FileNotFoundError("Prompt template not found: app/agents/x/prompts/generate.txt")).category is ErrorCategory.CONFIG_INVALID


def test_unrelated_file_not_found_is_not_config():
    # a FileNotFoundError with no prompt signal -> generic OS handling, not config
    assert classify(FileNotFoundError("[Errno 2] No such file or directory: '/tmp/data.bin'")).category is ErrorCategory.PERSISTENCE_IO


# --- 21. unexpected exceptions --------------------------

@pytest.mark.parametrize("exc", [KeyError("k"), AttributeError("a"), RuntimeError("r"), Exception("e"), TypeError("t")])
def test_unexpected_fallback(exc):
    res = classify(exc)
    assert res.category is ErrorCategory.UNEXPECTED
    assert res.code == "unexpected"
    assert res.log_level == logging.ERROR


def test_classify_never_raises_on_a_hostile_exception():
    class _Hostile(Exception):
        def __str__(self):  # pragma: no cover - exercised via classify
            raise RuntimeError("boom in __str__")

    res = classify(_Hostile())
    assert isinstance(res, AppError)  # no exception escaped


# --- 22. safe user messages (no leakage of internals) ----------

def test_no_message_contains_a_traceback_or_placeholder_or_brace():
    for exc in _REPRESENTATIVE.values():
        msg = classify(exc).user_message
        assert "Traceback" not in msg
        assert "{" not in msg and "}" not in msg
        assert "\n" not in msg
        assert 0 < len(msg) <= 300


# --- 23. fake API key / bearer / sk-token leakage prevention -----

_FAKE_KEY = "AIzaSyD" + "A" * 33
_FAKE_BEARER = "Authorization: Bearer sk-live-" + "0" * 32
_FAKE_SK = "sk-proj-" + "b" * 40


@pytest.mark.parametrize("payload", [
    f"Gemini API call failed: 401 unauthorized key={_FAKE_KEY}",
    f"Gemini API call failed: 429 RESOURCE_EXHAUSTED headers={{'{_FAKE_BEARER}'}}",
    f"Gemini API call failed: 503 unavailable token={_FAKE_SK}",
])
def test_secrets_never_reach_the_user_message(payload):
    msg = classify(_wrap(BusinessAnalystAgentError, payload)).user_message
    assert _FAKE_KEY not in msg
    assert "Bearer" not in msg and "sk-live-" not in msg and "sk-proj-" not in msg
    assert "Authorization" not in msg


# --- 24. validation / model-output marker leakage prevention -----

def test_model_output_values_never_reach_the_user_message():
    ve = _validation_error("SUPER_SECRET_MODEL_OUTPUT_VALUE_123")
    for exc in (ve, _wrap(TestCaseAgentError, f"Gemini structured call failed: {ve}", cause=ve),
                InvalidTestCaseJSONError("bad json around SUPER_SECRET_MODEL_OUTPUT_VALUE_123")):
        assert "SUPER_SECRET_MODEL_OUTPUT_VALUE_123" not in classify(exc).user_message


def test_parser_library_text_and_paths_never_reach_the_user_message():
    exc = ValueError("Could not read PDF file: mupdf: cannot open C:/Users/secret/report.pdf")
    msg = classify(exc).user_message
    assert msg == errmod._SPEC[ErrorCategory.INPUT_FILE][2]
    assert "mupdf" not in msg and "secret" not in msg and "report.pdf" not in msg


# --- 25. bounded cause-chain traversal --------------------------

def test_cause_chain_is_bounded_and_deep_causes_are_ignored():
    deep = VersionPersistenceError("deep corrupt beyond the horizon")
    e: BaseException = deep
    for _ in range(6):
        outer = RuntimeError("wrap")
        outer.__cause__ = e
        e = outer
    # `deep` now sits 6 links below `e` -> beyond _MAX_CAUSE_DEPTH -> not found
    assert classify(e).category is ErrorCategory.UNEXPECTED

    shallow = RuntimeError("wrap")
    shallow.__cause__ = VersionPersistenceError("shallow corrupt")
    assert classify(shallow).category is ErrorCategory.PERSISTENCE_CORRUPT


def test_cause_chain_is_cycle_safe():
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a  # cycle
    assert classify(a).category is ErrorCategory.UNEXPECTED  # terminates, no hang


# --- 26. retry classifier is NOT touched by 12B ------------------

def test_retry_classifier_and_markers_are_untouched():
    import app.agents.closure_report.agent as cra
    import app.agents.test_case.agent as tca

    for mod in (tca, cra):
        assert hasattr(mod, "_TRANSIENT_MARKERS")
        assert isinstance(mod._TRANSIENT_MARKERS, tuple)
        assert callable(mod._is_transient_llm_error)
    # the two agent copies remain identical to each other (unchanged from pre-12B)
    assert tca._TRANSIENT_MARKERS == cra._TRANSIENT_MARKERS
    # the taxonomy module does not import or reference the retry classifier
    src = Path(errmod.__file__).read_text(encoding="utf-8")
    assert "_TRANSIENT_MARKERS" not in src
    assert "_is_transient_llm_error" not in src


# --- log_app_error: safe, never raises, never leaks -----------

def test_log_app_error_persistence_corrupt_logs_frames_not_chained_validation_text(caplog):
    ve = _validation_error("CHAINED_VALUE_MUST_NOT_BE_LOGGED_777")
    vpe = VersionPersistenceError("Version history for 'p' is corrupt.")
    vpe.__cause__ = ve
    lg = logging.getLogger("app.utils.errors.test_probe")
    with caplog.at_level(logging.CRITICAL, logger=lg.name):
        log_app_error(lg, classify(vpe), vpe)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "PERSISTENCE_CORRUPT" in joined and "persistence.corrupt" in joined
    assert "CHAINED_VALUE_MUST_NOT_BE_LOGGED_777" not in joined


def test_log_app_error_schema_logs_type_names_not_validation_repr(caplog):
    ve = _validation_error("SCHEMA_REPR_MUST_NOT_BE_LOGGED_888")
    wrapped = _wrap(TestCaseAgentError, "Gemini structured call failed: <redacted>", cause=ve)
    lg = logging.getLogger("app.utils.errors.test_probe2")
    with caplog.at_level(logging.WARNING, logger=lg.name):
        log_app_error(lg, classify(wrapped), wrapped)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "LLM_SCHEMA" in joined and "cause=ValidationError" in joined
    assert "SCHEMA_REPR_MUST_NOT_BE_LOGGED_888" not in joined


def test_log_app_error_never_raises():
    class _Hostile(Exception):
        def __str__(self):  # pragma: no cover
            raise RuntimeError("boom")

    lg = logging.getLogger("app.utils.errors.test_probe3")
    log_app_error(lg, classify(_Hostile()), _Hostile())  # must not raise
