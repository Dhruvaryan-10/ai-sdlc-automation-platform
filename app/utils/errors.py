"""
Phase 12B — centralized error taxonomy.

A PURE, cheap, import-safe classifier that turns any exception the platform can
raise into a small `AppError` value: a stable `category` + `code`, a SAFE
user-facing `user_message`, a `log_level`, and advisory `retryable` metadata.

Consumed only by `app.ui.streamlit_app.friendly_error()` — the single funnel that
every UI action's error handling already goes through. Nothing here changes retry
behaviour, agent/service public APIs, artifact schemas, versioning semantics,
Gemini prompts, model/temperature, or the LangGraph.

`AppError` deliberately holds NO reference to the original exception — only safe
classification metadata — so it can never re-leak provider text, tracebacks,
`pydantic.ValidationError` input values, API keys, or artifact contents.

`classify(exc)`:
  * is pure and deterministic — no filesystem, no network, no service
    construction, no logging;
  * never raises (its whole body is guarded → `UNEXPECTED` on any internal
    error);
  * inspects `exc` and a bounded (`_MAX_CAUSE_DEPTH`) `__cause__` / `__context__`
    chain;
  * matches specific exception types before broad base classes — the ordering in
    `_classify_impl` is load-bearing.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from enum import Enum

from pydantic import ValidationError as _PydanticValidationError

# --- recognised project exception types (symbols only — importing these modules
#     does no new I/O beyond what importing `app.*` already does) ---------------
from app.agents.business_analyst.agent import BusinessAnalystAgentError as _BAAgentError
from app.agents.business_analyst.prompt_manager import (
    PromptRenderError as _PromptRenderError,
)
from app.agents.business_analyst.service import (
    BRDLockedError as _BRDLockedError,
    EmptyDocumentError as _EmptyDocumentError,
    UnsupportedFileTypeError as _UnsupportedFileTypeError,
)
from app.agents.closure_report.agent import ClosureReportAgentError as _CRAgentError
from app.agents.closure_report.service import (
    ClosureReportLockedError as _CRLockedError,
    InvalidClosureNarrativeError as _InvalidClosureNarrativeError,
    NoFinalBRDError as _NoFinalBRDError_CR,
)
from app.agents.initial_user_story.agent import (
    InitialUserStoryAgentError as _USAgentError,
)
from app.agents.initial_user_story.service import (
    NoFinalBRDError as _NoFinalBRDError_US,
    UserStoryLockedError as _UserStoryLockedError,
)
from app.agents.low_level_design.agent import LLDAgentError as _LLDAgentError
from app.agents.low_level_design.service import (
    LLDLockedError as _LLDLockedError,
    NoFinalHLDError as _NoFinalHLDError,
)
from app.agents.solution_architect.agent import (
    SolutionArchitectAgentError as _SAAgentError,
)
from app.agents.solution_architect.service import (
    HLDLockedError as _HLDLockedError,
    NoFinalBRDError as _NoFinalBRDError_SA,
)
from app.agents.test_case.agent import TestCaseAgentError as _TCAgentError
from app.agents.test_case.service import (
    InvalidTestCaseJSONError as _InvalidTestCaseJSONError,
    NoFinalBRDError as _NoFinalBRDError_TC,
    TestCaseLockedError as _TestCaseLockedError,
)
from app.agents.user_story_refinement.agent import (
    UserStoryRefinementAgentError as _USRAgentError,
)
from app.agents.user_story_refinement.service import (
    NoFinalBRDError as _NoFinalBRDError_USR,
    NoInitialUserStoriesError as _NoInitialUserStoriesError,
    RefinementLockedError as _RefinementLockedError,
)
from app.services.version_service import (
    VersionPersistenceError as _VersionPersistenceError,
)

_PREREQUISITE_TYPES: tuple[type[BaseException], ...] = (
    _NoFinalBRDError_SA, _NoFinalBRDError_US, _NoFinalBRDError_TC,
    _NoFinalBRDError_CR, _NoFinalBRDError_USR, _NoFinalHLDError,
    _NoInitialUserStoriesError,
)
_LOCK_TYPES: tuple[type[BaseException], ...] = (
    _BRDLockedError, _HLDLockedError, _UserStoryLockedError, _LLDLockedError,
    _RefinementLockedError, _TestCaseLockedError, _CRLockedError,
)
_AGENT_ERROR_TYPES: tuple[type[BaseException], ...] = (
    _BAAgentError, _SAAgentError, _USAgentError, _LLDAgentError,
    _USRAgentError, _TCAgentError, _CRAgentError,
)
_SCHEMA_VALUE_TYPES: tuple[type[BaseException], ...] = (
    _InvalidTestCaseJSONError, _InvalidClosureNarrativeError,
)


# --- taxonomy ---------------------------------------------------------------

class ErrorCategory(str, Enum):
    """Stable machine categories. The value IS the machine-readable code."""

    PERSISTENCE_CORRUPT = "persistence.corrupt"
    PERSISTENCE_IO = "persistence.io"
    LLM_QUOTA = "llm.quota"
    LLM_TIMEOUT = "llm.timeout"
    LLM_NETWORK = "llm.network"
    LLM_AUTH = "llm.auth"
    LLM_EMPTY_RESPONSE = "llm.empty_response"
    LLM_SCHEMA = "llm.schema"
    CONFIG_INVALID = "config.invalid"
    STATE_MISSING_PREREQUISITE = "state.missing_prerequisite"
    STATE_LOCKED = "state.locked"
    STATE_INVALID = "state.invalid"
    INPUT_FILE = "input.file"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class AppError:
    """Safe classification metadata only — never the original exception."""

    category: ErrorCategory
    code: str
    user_message: str
    log_level: int
    retryable: bool


# category -> (log_level, retryable, default_user_message)
_SPEC: dict[ErrorCategory, tuple[int, bool, str]] = {
    ErrorCategory.PERSISTENCE_CORRUPT: (
        logging.CRITICAL, False,
        "This project's saved history could not be read and no usable backup was "
        "available. Please restore the project's version history or contact support.",
    ),
    ErrorCategory.PERSISTENCE_IO: (
        logging.ERROR, False,
        "Couldn't read or write the project's saved history. Check folder "
        "permissions and try again.",
    ),
    ErrorCategory.LLM_QUOTA: (
        logging.WARNING, False,
        "Gemini is currently rate-limited or its quota may be exhausted. Wait a "
        "bit and try again.",
    ),
    ErrorCategory.LLM_TIMEOUT: (
        logging.WARNING, True,
        "Gemini took too long to respond. Try again, or increase "
        "GEMINI_TIMEOUT_SECONDS if this is a large document.",
    ),
    ErrorCategory.LLM_NETWORK: (
        logging.WARNING, True,
        "Couldn't reach Gemini. Check your network connection and try again.",
    ),
    ErrorCategory.LLM_AUTH: (
        logging.ERROR, False,
        "Gemini rejected the request. Check that GOOGLE_API_KEY in your .env is "
        "set correctly.",
    ),
    ErrorCategory.LLM_EMPTY_RESPONSE: (
        logging.WARNING, True,
        "Gemini returned an unusable response. Try generating again.",
    ),
    ErrorCategory.LLM_SCHEMA: (
        logging.WARNING, True,
        "Gemini's output did not match the required structure. Try generating again.",
    ),
    ErrorCategory.CONFIG_INVALID: (
        logging.ERROR, False,
        "The application appears to be misconfigured. Check the environment "
        "settings and prompt templates.",
    ),
    ErrorCategory.STATE_MISSING_PREREQUISITE: (
        logging.INFO, False,
        "A required earlier step has not been completed yet.",
    ),
    ErrorCategory.STATE_LOCKED: (
        logging.INFO, False,
        "That item is locked. Unlock it before making further changes.",
    ),
    ErrorCategory.STATE_INVALID: (
        logging.INFO, False,
        "That action can't be completed in the current state.",
    ),
    ErrorCategory.INPUT_FILE: (
        logging.INFO, False,
        "That file could not be used. Please upload a different DOCX, PDF, or TXT file.",
    ),
    ErrorCategory.UNEXPECTED: (
        logging.ERROR, False,
        "Something went wrong while processing that request. If the problem "
        "persists, check the application logs.",
    ),
}

# Categories whose message is an app-authored, safe, short string (never provider
# text / model output / secrets), so `log_app_error` may log a bounded `str(exc)`.
_SAFE_DETAIL_CATEGORIES = frozenset({
    ErrorCategory.STATE_MISSING_PREREQUISITE,
    ErrorCategory.STATE_LOCKED,
    ErrorCategory.STATE_INVALID,
})

# Generic fallback message, exposed for callers that need it without a category.
UNEXPECTED_MESSAGE = _SPEC[ErrorCategory.UNEXPECTED][2]


def _app_error(category: ErrorCategory, *, message: str | None = None) -> AppError:
    log_level, retryable, default_msg = _SPEC[category]
    return AppError(
        category=category,
        code=category.value,
        user_message=message if message else default_msg,
        log_level=log_level,
        retryable=retryable,
    )


# --- classification helpers (pure) --------------------------------------------

_MAX_CAUSE_DEPTH = 4

_PARSER_VALUEERROR_MARKERS = (
    "could not read pdf file", "could not read docx file",
    "could not read txt file", "could not decode txt file",
)

_AUTH_MARKERS = (
    "api key not valid", "api key invalid", "api-key-invalid", "invalid api key",
    "permission denied", "permission-denied", "unauthenticated", "unauthorized",
    "invalid authentication", "missing authentication",
)
_QUOTA_MARKERS = (
    "429", "resource exhausted", "resource has been exhausted", "rate limit",
    "ratelimit", "quota", "too many requests",
)
_TIMEOUT_MARKERS = (
    "timeout", "timed out", "deadline exceeded", "read timed out",
)
_NETWORK_MARKERS = (
    "503", "502", "504", "unavailable", "overloaded", "high demand",
    "service unavailable", "bad gateway", "server disconnected",
    "connection reset", "connection aborted", "connection refused",
    "remote protocol", "remoteprotocolerror", "incomplete read",
    "temporarily", "internal error", "internal server error", "try again",
)
_EMPTY_RESPONSE_MARKERS = (
    "returned an empty response", "returned no structured result",
    "no structured result", "structured result contained no",
    "was not a closurenarrative", "returned an unusable",
)

_HTTPX_TIMEOUT_NAMES = frozenset({
    "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
    "TimeoutException",
})
_HTTPX_NETWORK_NAMES = frozenset({
    "ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
    "ProtocolError", "NetworkError", "CloseError",
})
_PROVIDER_TYPE_NAMES = frozenset({
    "ChatGoogleGenerativeAIError", "GoogleGenerativeAIError",
    "ClientError", "ServerError", "APIError", "APIStatusError",
    "GoogleAPICallError", "GoogleAPIError",
    "ResourceExhausted", "DeadlineExceeded", "ServiceUnavailable",
    "InternalServerError", "PermissionDenied", "Unauthenticated",
    "TooManyRequests",
}) | _HTTPX_TIMEOUT_NAMES | _HTTPX_NETWORK_NAMES


def _cause_chain(exc: BaseException) -> list[BaseException]:
    """`exc` plus up to `_MAX_CAUSE_DEPTH - 1` `__cause__` / `__context__`
    ancestors (cycle-safe, bounded)."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and len(chain) < _MAX_CAUSE_DEPTH and id(cur) not in seen:
        chain.append(cur)
        seen.add(id(cur))
        nxt = cur.__cause__
        if nxt is None and not getattr(cur, "__suppress_context__", False):
            nxt = cur.__context__
        cur = nxt
    return chain


def _type_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def _status_code(exc: BaseException):
    """Best-effort HTTP/grpc status from safe attributes; int or upper-case name
    string, else None."""
    for attr in ("code", "status_code", "grpc_status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
        name = getattr(val, "name", None)
        if isinstance(name, str):
            return name.upper()
    resp = getattr(exc, "response", None)
    val = getattr(resp, "status_code", None)
    return val if isinstance(val, int) else None


def _blob(exc: BaseException) -> str:
    """Lowercased 'TypeName: message' for marker matching ONLY — never logged,
    never returned to a user."""
    try:
        text = str(exc)
    except Exception:  # pragma: no cover
        text = ""
    return f"{type(exc).__name__}: {text}".lower().replace("_", " ")


def _clean_message(exc: BaseException) -> str:
    """A single-line, length-bounded `str(exc)` for the app-authored (already
    safe) prerequisite / lock / workflow-guard messages."""
    try:
        return " ".join(str(exc).split())[:300].strip()
    except Exception:  # pragma: no cover
        return ""


def _looks_like_prompt_file(exc: BaseException) -> bool:
    blob = _blob(exc)
    return "prompt" in blob or (".txt" in blob and "prompts" in blob)


def _is_parser_valueerror(exc: BaseException) -> bool:
    blob = _blob(exc)
    return any(marker in blob for marker in _PARSER_VALUEERROR_MARKERS)


def _looks_like_provider(chain: list[BaseException]) -> bool:
    for exc in chain:
        if _type_names(exc) & _PROVIDER_TYPE_NAMES:
            return True
        if isinstance(exc, TimeoutError):
            return True
    return False


def _classify_llm(chain: list[BaseException]) -> AppError:
    """Sub-classify a wrapped Gemini/provider failure. Prefers structured status
    codes, falls back to conservative message markers."""
    codes = [c for c in (_status_code(e) for e in chain) if c is not None]
    names: set[str] = set()
    for exc in chain:
        names |= _type_names(exc)
    blobs = [_blob(exc) for exc in chain]

    def marker(markers) -> bool:
        return any(m in b for b in blobs for m in markers)

    if (any(c in (401, 403) for c in codes)
            or "PERMISSION_DENIED" in codes or "UNAUTHENTICATED" in codes
            or marker(_AUTH_MARKERS)):
        return _app_error(ErrorCategory.LLM_AUTH)
    if 429 in codes or "RESOURCE_EXHAUSTED" in codes or marker(_QUOTA_MARKERS):
        return _app_error(ErrorCategory.LLM_QUOTA)
    if (names & _HTTPX_TIMEOUT_NAMES
            or any(isinstance(e, TimeoutError) for e in chain)
            or "DEADLINE_EXCEEDED" in codes or marker(_TIMEOUT_MARKERS)):
        return _app_error(ErrorCategory.LLM_TIMEOUT)
    if (names & _HTTPX_NETWORK_NAMES
            or any(c in (500, 502, 503, 504) for c in codes)
            or "UNAVAILABLE" in codes or "INTERNAL" in codes or "ABORTED" in codes
            or marker(_NETWORK_MARKERS)):
        return _app_error(ErrorCategory.LLM_NETWORK)
    if marker(_EMPTY_RESPONSE_MARKERS):
        return _app_error(ErrorCategory.LLM_EMPTY_RESPONSE)
    # a wrapped Gemini failure we cannot sub-classify: the safest actionable
    # message is the connectivity one (matches the pre-12B generic wording).
    return _app_error(ErrorCategory.LLM_NETWORK)


# --- public entry point -----------------------------------------------------

def classify(exc: BaseException) -> AppError:
    """Classify `exc` into an `AppError`. Pure, deterministic, never raises."""
    try:
        return _classify_impl(exc)
    except Exception:  # pragma: no cover - classification must never break callers
        return _app_error(ErrorCategory.UNEXPECTED)


def _classify_impl(exc: BaseException) -> AppError:
    chain = _cause_chain(exc)

    def has(types) -> bool:
        return any(isinstance(e, types) for e in chain)

    def first(types) -> BaseException | None:
        return next((e for e in chain if isinstance(e, types)), None)

    # 1. persistence corruption — BEFORE any RuntimeError / OSError handling.
    if has(_VersionPersistenceError):
        return _app_error(ErrorCategory.PERSISTENCE_CORRUPT)

    # 2. dedicated input/file errors (keep their existing safe wording).
    if has(_UnsupportedFileTypeError):
        return _app_error(
            ErrorCategory.INPUT_FILE,
            message="That file type isn't supported. Please upload a DOCX, PDF, or TXT file.",
        )
    if has(_EmptyDocumentError):
        return _app_error(
            ErrorCategory.INPUT_FILE,
            message=("No readable text was found in that document. If it's a "
                     "scanned PDF, please upload a text-based version instead."),
        )

    # 3. missing prerequisite — preserve the clean str(exc) message.
    prereq = first(_PREREQUISITE_TYPES)
    if prereq is not None:
        return _app_error(
            ErrorCategory.STATE_MISSING_PREREQUISITE, message=_clean_message(prereq)
        )

    # 4. locked / workflow-state — preserve the clean str(exc) message.
    locked = first(_LOCK_TYPES)
    if locked is not None:
        return _app_error(ErrorCategory.STATE_LOCKED, message=_clean_message(locked))

    # 5. service-level structured-output validation (ValueError subclasses — so
    #    this MUST precede steps 7 and 9).
    if has(_SCHEMA_VALUE_TYPES):
        return _app_error(ErrorCategory.LLM_SCHEMA)

    # 6. prompt / configuration errors.
    if has(_PromptRenderError):
        return _app_error(ErrorCategory.CONFIG_INVALID)
    fnf = first((FileNotFoundError,))
    if fnf is not None and _looks_like_prompt_file(fnf):
        return _app_error(ErrorCategory.CONFIG_INVALID)

    # 7. pydantic schema validation anywhere in the bounded chain.
    if has(_PydanticValidationError):
        return _app_error(ErrorCategory.LLM_SCHEMA)

    # 8. wrapped Gemini / provider failures.
    if first(_AGENT_ERROR_TYPES) is not None or _looks_like_provider(chain):
        return _classify_llm(chain)

    # 9. generic OS / value errors.
    if has((PermissionError, OSError)):
        return _app_error(ErrorCategory.PERSISTENCE_IO)
    verr = first((ValueError,))
    if verr is not None:
        if _is_parser_valueerror(verr):
            return _app_error(ErrorCategory.INPUT_FILE)  # generic wording, no raw text
        return _app_error(ErrorCategory.STATE_INVALID, message=_clean_message(verr))

    # 10. fallback.
    return _app_error(ErrorCategory.UNEXPECTED)


# --- safe diagnostic logging (used by friendly_error) -----------------------

def log_app_error(logger_: logging.Logger, classified: AppError, exc: BaseException) -> None:
    """Emit ONE safe diagnostic record for `classified`.

    NEVER logs: `str()` of a `pydantic.ValidationError` or of a
    `VersionPersistenceError` chain, chained provider exception text, prompts, or
    artifact content. Logs: the stable category + code, the exception type name,
    the immediate cause type name, and — only for the app-authored
    prerequisite / lock / workflow messages — a length-bounded single-line
    `str(exc)`. A call-stack traceback is attached only where it is both useful
    and provably free of `ValidationError` values (see `classify` ordering);
    `persistence.corrupt` gets frames only (never the chained cause's text).
    """
    try:
        cause = exc.__cause__
        if cause is None and not getattr(exc, "__suppress_context__", False):
            cause = exc.__context__
        cause_name = type(cause).__name__ if cause is not None else "none"
        head = (
            f"{classified.category.name} [{classified.code}] "
            f"type={type(exc).__name__} cause={cause_name}"
        )

        if classified.category in _SAFE_DETAIL_CATEGORIES:
            logger_.log(
                classified.log_level, "%s detail=%s", head, _clean_message(exc)
            )
        elif classified.category is ErrorCategory.PERSISTENCE_CORRUPT:
            frames = (
                "".join(traceback.format_tb(exc.__traceback__))
                if exc.__traceback__ else ""
            )
            logger_.critical("%s%s", head, ("\n" + frames) if frames else "")
        elif classified.log_level >= logging.ERROR:
            logger_.log(classified.log_level, "%s", head, exc_info=True)
        else:
            logger_.log(classified.log_level, "%s", head)
    except Exception:  # pragma: no cover - logging must never break error handling
        try:
            logger_.error(
                "log_app_error failed for code=%s", getattr(classified, "code", "?")
            )
        except Exception:
            pass
