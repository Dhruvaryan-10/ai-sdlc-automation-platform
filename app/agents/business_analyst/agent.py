"""
Business Analyst Agent.

WHY LANGCHAIN AS AN ABSTRACTION LAYER:
We call Gemini through LangChain's ChatGoogleGenerativeAI wrapper rather than
Google's raw SDK directly. This means if Phase 2+ ever needs to swap models
(e.g. add an OpenAI fallback, or move to Claude), only this one class needs
to change — everything above it (service layer, UI) talks to
`BusinessAnalystAgent`, not to Gemini specifics.

This class has exactly two responsibilities:
    1. generate_brd()  -> first-draft BRD from a clean SOW
    2. refine_brd()     -> apply targeted feedback to an existing BRD

It does NOT know about file parsing, versioning, or the UI. That separation
is what makes this testable in isolation.
"""

import random
import time

from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from app.agents.business_analyst.prompt_manager import PromptManager
from app.utils.llm import build_chat_llm
from app.utils.logger import get_logger
from app.utils.metrics import instrumented_invoke

logger = get_logger(__name__)

# --- transient-failure retry for the Gemini call (Phase 16) -----------------
# Replicated VERBATIM from app/agents/test_case/agent.py (Phases 11A / 13A):
# Gemini occasionally returns "503 UNAVAILABLE" / "model overloaded" / rate-limit
# errors under load. Those are worth a couple of quick retries; validation or
# bad-request errors are not. Kept deliberately small: 3 attempts total, a few
# seconds of backoff at most, stdlib only (no new dependency). Duplicated per
# agent on purpose -- CLAUDE.md forbids a shared retry helper / base class until
# a dedicated cleanup phase.
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_S = 1.0
_RETRY_MAX_DELAY_S = 8.0

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRYABLE_GRPC_NAMES = {
    "UNAVAILABLE", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED", "INTERNAL", "ABORTED",
}
_TRANSIENT_MARKERS = (
    "503", "429",
    "unavailable", "overloaded", "high demand", "try again",
    "temporarily", "deadline exceeded", "timed out", "timeout",
    "rate limit", "ratelimit", "resource exhausted",
    "resource has been exhausted", "service unavailable", "internal error",
    "remoteprotocolerror", "remote protocol", "server disconnected",
    "connection reset", "connectionreseterror",
    "connection aborted", "connectionabortederror",
    "incomplete read", "incompleteread",
)


def _is_transient_llm_error(exc: BaseException) -> bool:
    """True for errors worth retrying (transient server/capacity failures).

    Schema/validation errors and other clearly non-transient application errors
    return False so they are surfaced immediately without wasted retries.
    """
    if isinstance(exc, ValidationError):
        return False

    for attr in ("code", "status_code", "grpc_status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and val in _RETRYABLE_STATUS_CODES:
            return True
        name = getattr(val, "name", None)  # e.g. grpc.StatusCode.UNAVAILABLE
        if isinstance(name, str) and name.upper() in _RETRYABLE_GRPC_NAMES:
            return True

    blob = f"{type(exc).__name__}: {exc}".lower()
    blob_spaced = blob.replace("_", " ")  # normalise grpc names like DEADLINE_EXCEEDED
    return any(
        marker in blob or marker in blob_spaced for marker in _TRANSIENT_MARKERS
    )


def _retry_backoff_seconds(attempt: int) -> float:
    """Exponential backoff with 'equal jitter': delay in [d/2, d] where
    d = min(max_delay, base * 2**(attempt-1))."""
    ceiling = min(_RETRY_MAX_DELAY_S, _RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
    return ceiling / 2 + random.uniform(0, ceiling / 2)


@dataclass
class ProjectMetadata:
    """Metadata about the project the SOW belongs to.

    Kept as an explicit dataclass (rather than a loose dict) so callers get
    autocomplete + type checking, and so PromptManager placeholder mismatches
    are caught early rather than at runtime string-formatting time.
    """

    project_name: str
    client_name: str
    project_type: str
    industry: str
    language: str = "English"
    output_format: str = "Markdown"


class BusinessAnalystAgentError(Exception):
    """Raised when the Gemini call fails or returns an unusable response."""


class BusinessAnalystAgent:
    """Wraps Gemini (via LangChain) to generate and refine BRDs."""

    _TELEMETRY_STAGE = "brd"  # Phase 13A: SDLC stage tag for LLM telemetry

    def __init__(self, prompt_manager: PromptManager | None = None):
        self._prompt_manager = prompt_manager or PromptManager()
        # Phase 11A: the Gemini client is built lazily on the first real
        # generate/refine call, not here — constructing this service for a
        # read-only path must not pay the ~1.3 s client-construction cost.
        self._llm = None

    def _ensure_llm(self):
        """Build the Gemini client on first use, then reuse it."""
        if self._llm is None:
            self._llm = build_chat_llm()
        return self._llm

    @staticmethod
    def _extract_text(content) -> str:
        """Extract plain text from an LLM response's `.content`.

        Newer versions of langchain-google-genai (and Gemini 3+ models) return
        `.content` as a dict or list of structured content blocks — e.g.
        {"type": "text", "text": "...", "extras": {...}} — rather than a plain
        string. Older versions returned a plain string directly. This
        normalizes all of these shapes into a single plain-text string.
        """
        if isinstance(content, str):
            return content

        if isinstance(content, dict):
            if "text" in content:
                return str(content.get("text", ""))
            return ""

        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict) and "text" in block:
                    text_parts.append(str(block.get("text", "")))
            return "\n".join(text_parts)

        return str(content) if content else ""

    def _invoke(self, prompt: str) -> str:
        """Invoke Gemini and return the normalized response text.

        Transient Gemini failures (503 UNAVAILABLE / overloaded / rate-limit /
        transport disconnects) are retried up to ``_RETRY_MAX_ATTEMPTS`` times
        with exponential backoff + jitter. Non-transient errors (validation, bad
        request, auth) are surfaced immediately. The ``BusinessAnalystAgentError`` contract -- and the
        empty-response check below (never retried) -- is unchanged. The attempt
        number is forwarded to ``instrumented_invoke`` so telemetry stays correct.
        """
        response = None
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                response = instrumented_invoke(
                    self._ensure_llm(), prompt,
                    stage=self._TELEMETRY_STAGE, attempt=attempt,
                )
                break
            except Exception as exc:
                if attempt < _RETRY_MAX_ATTEMPTS and _is_transient_llm_error(exc):
                    delay = _retry_backoff_seconds(attempt)
                    logger.warning(
                        "Gemini call failed (attempt %d/%d), retrying in %.1fs: %s",
                        attempt, _RETRY_MAX_ATTEMPTS, delay, exc,
                    )
                    time.sleep(delay)
                    continue
                logger.error(f"Gemini API call failed: {exc}")
                raise BusinessAnalystAgentError(f"Gemini API call failed: {exc}") from exc

        raw_content = getattr(response, "content", None)
        text = self._extract_text(raw_content).strip()

        if not text:
            logger.error("Gemini returned an empty response")
            raise BusinessAnalystAgentError("Gemini returned an empty response")

        return text

    def generate_brd(self, clean_sow: str, metadata: ProjectMetadata) -> str:
        """Generate BRD Version 1 from a cleaned SOW."""
        if not clean_sow or not clean_sow.strip():
            raise ValueError("Cannot generate a BRD from empty SOW text")

        prompt = self._prompt_manager.render(
            "generate_brd.txt",
            sow_text=clean_sow,
            project_name=metadata.project_name,
            client_name=metadata.client_name,
            project_type=metadata.project_type,
            industry=metadata.industry,
            language=metadata.language,
            output_format=metadata.output_format,
            generated_date=date.today().isoformat(),
        )

        logger.info(f"Generating BRD v1 for project '{metadata.project_name}'")
        brd_text = self._invoke(prompt)
        logger.info(f"BRD v1 generated ({len(brd_text)} chars)")
        return brd_text

    def refine_brd(self, current_brd: str, user_feedback: str, current_version: int) -> str:
        """Apply targeted feedback to an existing BRD and return the full updated document."""
        if not current_brd or not current_brd.strip():
            raise ValueError("Cannot refine an empty BRD")
        if not user_feedback or not user_feedback.strip():
            raise ValueError("Refinement feedback cannot be empty")

        prompt = self._prompt_manager.render(
            "refine_brd.txt",
            current_brd=current_brd,
            user_feedback=user_feedback,
            current_version=str(current_version),
        )

        logger.info(f"Refining BRD from v{current_version} with feedback: '{user_feedback[:80]}...'")
        refined_text = self._invoke(prompt)
        logger.info(f"BRD refined ({len(refined_text)} chars)")
        return refined_text