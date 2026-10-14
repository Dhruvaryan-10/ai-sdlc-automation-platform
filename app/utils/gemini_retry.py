"""
Gemini failure classification + a bounded, server-aware retry policy.

WHY THIS FILE EXISTS (free-tier stabilisation):
Every generation agent used to decide "retry or not" purely from message
markers, and retried 429 RESOURCE_EXHAUSTED exactly like a 503 — after 0.5–2 s,
up to three times — while the Gemini SDK underneath retried each of those
attempts again (``GEMINI_MAX_RETRIES=2`` = two HTTP requests per attempt). On the
free tier (5 requests / minute / model) one click could therefore send six
requests in a few seconds, each one burning quota, while Google's response was
explicitly asking the client to wait ~45 s.

This module is the single place that understands Google's error shapes:

* ``classify_gemini_failure(exc)`` walks a bounded ``__cause__`` / ``__context__``
  chain (LangChain wraps 429s as ``ChatGoogleGenerativeAIError`` *from* the
  ``google.genai`` ``ClientError``; 5xx arrive as the raw ``ServerError``) and
  returns a ``GeminiFailure`` with a kind (per-minute rate limit, daily quota,
  unknown-window quota, temporarily unavailable, model/config error, other),
  the HTTP status, and Google's requested retry delay when one is present.
* ``extract_retry_delay_seconds(exc)`` reads, in order: the structured
  ``google.rpc.RetryInfo.retryDelay`` in the error details, the ``Retry-After``
  response header, then the "Please retry in 42.7s" message text.
* ``GeminiRetryPolicy`` is a small per-call object the agents' existing retry
  loops consult instead of the old ``attempt < MAX and is_transient(exc)``
  condition. It never sleeps itself and never loops — the agent loop stays the
  only loop, so the overall budget remains: ``attempts <=
  GEMINI_RETRY_MAX_ATTEMPTS`` and ``total sleep <= GEMINI_RETRY_MAX_WAIT_SECONDS``.

Pure and import-safe: no network, no Streamlit, no ``app.agents`` imports (so
``app.utils.errors`` can import it without a cycle). ``settings`` is read lazily
at call time so tests can monkeypatch it. Nothing here ever logs or returns
provider text — ``describe()`` produces a bounded, secret-free summary.
"""

from __future__ import annotations

import email.utils
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

__all__ = [
    "GeminiFailureKind",
    "GeminiFailure",
    "GeminiRetryPolicy",
    "classify_gemini_failure",
    "extract_retry_delay_seconds",
    "describe",
]

_MAX_CHAIN_DEPTH = 6
# A server-requested delay longer than a day is not a "retry window" — ignore it.
_MAX_SANE_DELAY_S = 86_400.0


class GeminiFailureKind(str, Enum):
    RATE_LIMIT = "rate_limit"      # short-window (per-minute) 429
    DAILY_QUOTA = "daily_quota"    # per-day quota exhausted — never auto-retried
    QUOTA = "quota"                # 429 / RESOURCE_EXHAUSTED, window unknown
    UNAVAILABLE = "unavailable"    # 5xx / UNAVAILABLE / "high demand"
    MODEL_CONFIG = "model_config"  # unknown / unsupported model name (404 / 400)
    OTHER = "other"                # anything else (decided by the agent's own rules)


_QUOTA_KINDS = frozenset({
    GeminiFailureKind.RATE_LIMIT, GeminiFailureKind.DAILY_QUOTA, GeminiFailureKind.QUOTA,
})


@dataclass(frozen=True)
class GeminiFailure:
    kind: GeminiFailureKind
    status_code: int | None = None
    retry_after_seconds: float | None = None

    @property
    def is_quota(self) -> bool:
        return self.kind in _QUOTA_KINDS


# --- markers (matched against lowercased text with "_" -> " "; never logged) ---

_QUOTA_MARKERS = (
    "429", "resource exhausted", "resource has been exhausted", "quota",
    "rate limit", "ratelimit", "too many requests",
)
_DAILY_MARKERS = (
    "perday", "per day", "requests per day", "requestsperday", "daily",
)
_MINUTE_MARKERS = (
    "perminute", "per minute", "requests per minute", "requestsperminute",
    "tokensperminute", "tokens per minute",
)
_UNAVAILABLE_MARKERS = (
    "503", "502", "504", "unavailable", "overloaded", "high demand",
    "service unavailable", "bad gateway", "internal server error",
)
_MODEL_CONFIG_MARKERS = (
    "is not found for api version", "is not supported for generatecontent",
    "model not found", "unknown model", "invalid model", "models/",
)

_UNAVAILABLE_STATUS_NAMES = frozenset({"UNAVAILABLE", "INTERNAL"})

# "Please retry in 42.7s." / "retry in 49s" / "retry after 30 seconds"
_RETRY_IN_RE = re.compile(
    r"retry\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|sec|secs|seconds?)\b",
    re.IGNORECASE,
)
# "'retryDelay': '42s'" (dict repr / JSON) and "retry_delay { seconds: 42 }"
_RETRY_DELAY_FIELD_RE = re.compile(
    r"retry_?delay['\"]?\s*[:=]?\s*['\"]?(\d+(?:\.\d+)?)s\b", re.IGNORECASE,
)
_RETRY_DELAY_PROTO_RE = re.compile(
    r"retry_?delay\s*\{\s*seconds\s*:\s*(\d+)", re.IGNORECASE,
)
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*s\s*$")


# --- chain helpers ------------------------------------------------------------

def _chain(exc: BaseException | None) -> list[BaseException]:
    out: list[BaseException] = []
    seen: set[int] = set()
    cur = exc
    while cur is not None and len(out) < _MAX_CHAIN_DEPTH and id(cur) not in seen:
        out.append(cur)
        seen.add(id(cur))
        nxt = cur.__cause__
        if nxt is None and not getattr(cur, "__suppress_context__", False):
            nxt = cur.__context__
        cur = nxt
    return out


def _int_code(exc: BaseException) -> int | None:
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and not isinstance(val, bool) and 100 <= val <= 599:
            return val
    resp = getattr(exc, "response", None)
    val = getattr(resp, "status_code", None)
    if isinstance(val, int) and not isinstance(val, bool) and 100 <= val <= 599:
        return val
    return None


def _status_name(exc: BaseException) -> str | None:
    status = getattr(exc, "status", None)
    if isinstance(status, str) and status:
        return status.upper()
    for attr in ("code", "grpc_status_code"):
        name = getattr(getattr(exc, attr, None), "name", None)
        if isinstance(name, str) and name:
            return name.upper()
    return None


def _text(exc: BaseException) -> str:
    try:
        raw = str(exc)
    except Exception:  # pragma: no cover - defensive
        raw = ""
    return f"{type(exc).__name__}: {raw}".lower().replace("_", " ")


def _detail_entries(exc: BaseException) -> list[dict]:
    """``error.details`` entries from a ``google.genai`` APIError (``.details`` is
    the parsed JSON body). Anything unexpected yields ``[]``."""
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return []
    err = details.get("error", details)
    entries = err.get("details") if isinstance(err, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _quota_ids(entries: Iterable[dict]) -> list[str]:
    ids: list[str] = []
    for entry in entries:
        for violation in entry.get("violations") or []:
            if isinstance(violation, dict):
                for key in ("quotaId", "quota_id", "quotaMetric", "quota_metric"):
                    val = violation.get(key)
                    if isinstance(val, str):
                        ids.append(val.lower().replace("_", " "))
    return ids


# --- retry-delay extraction --------------------------------------------------

def _sane(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0 or value > _MAX_SANE_DELAY_S:  # NaN / negative / absurd
        return None
    return value


def _parse_duration(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _sane(value)
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match:
            return _sane(float(match.group(1)))
    if isinstance(value, dict):  # {"seconds": 42, "nanos": 500000000}
        seconds = value.get("seconds", 0)
        nanos = value.get("nanos", 0)
        try:
            return _sane(float(seconds) + float(nanos) / 1e9)
        except (TypeError, ValueError):
            return None
    return None


def _from_retry_info(entries: Iterable[dict]) -> float | None:
    for entry in entries:
        type_url = str(entry.get("@type", ""))
        if "RetryInfo" in type_url or "retryDelay" in entry or "retry_delay" in entry:
            parsed = _parse_duration(entry.get("retryDelay", entry.get("retry_delay")))
            if parsed is not None:
                return parsed
    return None


def _from_retry_after_header(exc: BaseException, *, now: float | None = None) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    try:
        return _sane(float(raw))
    except ValueError:
        pass
    try:  # HTTP-date form
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    current = time.time() if now is None else now
    return _sane(max(0.0, when.timestamp() - current))


def _from_text(text: str) -> float | None:
    match = _RETRY_IN_RE.search(text)
    if match:
        value = float(match.group(1))
        if match.group(2).lower().startswith("m"):
            value /= 1000.0
        return _sane(value)
    for pattern in (_RETRY_DELAY_FIELD_RE, _RETRY_DELAY_PROTO_RE):
        match = pattern.search(text)
        if match:
            return _sane(float(match.group(1)))
    return None


def extract_retry_delay_seconds(exc: BaseException) -> float | None:
    """Google's requested retry delay in seconds, or ``None`` when absent.

    Structured ``RetryInfo`` wins over the ``Retry-After`` header, which wins over
    message text. Never raises.
    """
    try:
        chain = _chain(exc)
        for link in chain:
            parsed = _from_retry_info(_detail_entries(link))
            if parsed is not None:
                return parsed
        for link in chain:
            parsed = _from_retry_after_header(link)
            if parsed is not None:
                return parsed
        for link in chain:
            try:
                raw = str(link)
            except Exception:  # pragma: no cover
                continue
            parsed = _from_text(raw)
            if parsed is not None:
                return parsed
    except Exception:  # pragma: no cover - extraction must never break callers
        return None
    return None


# --- classification -----------------------------------------------------------

def classify_gemini_failure(exc: BaseException) -> GeminiFailure:
    """Classify a Gemini/provider failure. Pure, deterministic, never raises."""
    try:
        return _classify(exc)
    except Exception:  # pragma: no cover - defensive
        return GeminiFailure(GeminiFailureKind.OTHER)


def _classify(exc: BaseException) -> GeminiFailure:
    chain = _chain(exc)
    codes = [c for c in (_int_code(e) for e in chain) if c is not None]
    names = {n for n in (_status_name(e) for e in chain) if n}
    texts = [_text(e) for e in chain]
    entries = [entry for e in chain for entry in _detail_entries(e)]
    quota_ids = _quota_ids(entries)
    status = codes[0] if codes else None

    def has(markers: Iterable[str], haystacks: Iterable[str] | None = None) -> bool:
        pool = list(haystacks) if haystacks is not None else texts
        return any(m in h for h in pool for m in markers)

    is_quota = 429 in codes or "RESOURCE_EXHAUSTED" in names or has(_QUOTA_MARKERS)
    if is_quota:
        delay = extract_retry_delay_seconds(exc)
        status = 429 if 429 in codes else status
        # Structured quota ids are authoritative; fall back to message text.
        daily = has(_DAILY_MARKERS, quota_ids) or (not quota_ids and has(_DAILY_MARKERS))
        minute = has(_MINUTE_MARKERS, quota_ids) or (not quota_ids and has(_MINUTE_MARKERS))
        if daily:
            # Retrying cannot help until the daily reset — never auto-retry,
            # even if a short retry delay was also included.
            return GeminiFailure(GeminiFailureKind.DAILY_QUOTA, status, delay)
        if minute or delay is not None:
            return GeminiFailure(GeminiFailureKind.RATE_LIMIT, status, delay)
        return GeminiFailure(GeminiFailureKind.QUOTA, status, None)

    if (any(500 <= c <= 504 for c in codes) or names & _UNAVAILABLE_STATUS_NAMES
            or has(_UNAVAILABLE_MARKERS)):
        return GeminiFailure(
            GeminiFailureKind.UNAVAILABLE, status, extract_retry_delay_seconds(exc)
        )

    mentions_model = has(("model",))
    if (404 in codes and mentions_model) or (
        (400 in codes or 404 in codes or "NOT_FOUND" in names or "INVALID_ARGUMENT" in names)
        and has(_MODEL_CONFIG_MARKERS)
    ) or has(("is not found for api version", "is not supported for generatecontent")):
        return GeminiFailure(GeminiFailureKind.MODEL_CONFIG, status, None)

    return GeminiFailure(GeminiFailureKind.OTHER, status, None)


def describe(exc: BaseException, failure: GeminiFailure | None = None) -> str:
    """A bounded, secret-free one-line summary for logs — never provider text."""
    try:
        failure = failure or classify_gemini_failure(exc)
        parts = [
            f"kind={failure.kind.value}",
            f"status={failure.status_code if failure.status_code is not None else '-'}",
            f"type={type(exc).__name__}",
        ]
        cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        if cause is not None:
            parts.append(f"cause={type(cause).__name__}")
        if failure.retry_after_seconds is not None:
            parts.append(f"retry_after={failure.retry_after_seconds:.1f}s")
        return " ".join(parts)
    except Exception:  # pragma: no cover
        return f"type={type(exc).__name__}"


# --- per-call retry policy ---------------------------------------------------------

def _settings():
    from app.utils.config import settings  # lazy: read at call time

    return settings


class GeminiRetryPolicy:
    """Decides whether one failed Gemini attempt should be retried, and after how
    long. One instance per logical call (it remembers what it already spent).

    Rules (all bounded):
      * never beyond ``max_attempts`` total attempts;
      * DAILY_QUOTA, unknown-window QUOTA, MODEL_CONFIG: never retried;
      * RATE_LIMIT: retried at most ``max_rate_limit_retries`` times (default 1),
        and only when Google supplied a retry delay that fits in the remaining
        wait budget — otherwise the error surfaces immediately so the UI can tell
        the user how long to wait (no blocking a Streamlit request for minutes);
      * UNAVAILABLE / other transient errors (per the agent's own
        ``is_transient``): the agent's exponential backoff with jitter, raised to
        any valid server-provided delay;
      * the sum of all sleeps never exceeds ``max_wait_seconds``.
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        is_transient: Callable[[BaseException], bool],
        backoff: Callable[[int], float],
        max_wait_seconds: float | None = None,
        max_rate_limit_retries: int = 1,
        rate_limit_jitter: Callable[[], float] | None = None,
    ):
        self.max_attempts = max(1, int(max_attempts))
        self._is_transient = is_transient
        self._backoff = backoff
        if max_wait_seconds is None:
            max_wait_seconds = float(_settings().gemini_retry_max_wait_seconds)
        self.max_wait_seconds = max(0.0, float(max_wait_seconds))
        self._max_rate_limit_retries = max(0, int(max_rate_limit_retries))
        self._rate_limit_jitter = rate_limit_jitter or (lambda: random.uniform(0.5, 2.0))
        self.waited_seconds = 0.0
        self.rate_limit_retries = 0
        self.last_failure: GeminiFailure | None = None

    @property
    def remaining_wait_seconds(self) -> float:
        return max(0.0, self.max_wait_seconds - self.waited_seconds)

    def next_delay(self, exc: BaseException, attempt: int) -> float | None:
        """Seconds to sleep before the next attempt, or ``None`` to stop and
        surface ``exc``. Never raises."""
        try:
            failure = classify_gemini_failure(exc)
            self.last_failure = failure
            if attempt >= self.max_attempts:
                return None

            if failure.kind in (GeminiFailureKind.DAILY_QUOTA, GeminiFailureKind.QUOTA,
                                GeminiFailureKind.MODEL_CONFIG):
                return None

            if failure.kind is GeminiFailureKind.RATE_LIMIT:
                if self.rate_limit_retries >= self._max_rate_limit_retries:
                    return None
                if failure.retry_after_seconds is None:
                    return None  # no server hint: don't guess, let the user retry
                delay = failure.retry_after_seconds + max(0.0, self._rate_limit_jitter())
                if delay > self.remaining_wait_seconds:
                    return None
                self.rate_limit_retries += 1
                self.waited_seconds += delay
                return delay

            if not self._is_transient(exc):
                return None
            delay = max(0.0, float(self._backoff(attempt)))
            if failure.retry_after_seconds is not None:
                delay = max(delay, failure.retry_after_seconds)
            if delay > self.remaining_wait_seconds:
                return None
            self.waited_seconds += delay
            return delay
        except Exception:  # pragma: no cover - a policy bug must not cause retries
            return None
