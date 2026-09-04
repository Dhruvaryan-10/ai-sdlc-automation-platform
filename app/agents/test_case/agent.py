"""
QA / Test Case Agent (Phase 6).

Built structurally parallel to the earlier agents (BusinessAnalystAgent /
SolutionArchitectAgent / InitialUserStoryAgent / LowLevelDesignAgent /
UserStoryRefinementAgent) — same LangChain/Gemini wrapper pattern, same
external-prompt approach, same response normalization — rather than sharing an
abstract base class. Consolidating the duplicated `_extract_text` / `_invoke`
across the six agents is a deliberate deferred cleanup, not part of Phase 6.

This class has exactly two responsibilities:
    1. generate_test_cases() -> a full test-case set from the available artifacts
    2. refine_test_cases()   -> apply reviewer feedback to an existing set

Both return a JSON *string* (the prompts require JSON-only output). Parsing,
validation, markdown rendering, versioning, provenance and staleness all live in
TestCaseService. This agent knows nothing about persistence or the UI, and it
imports no other agent package's implementation (only the shared
ProjectMetadata value type, PromptManager infrastructure, and this package's own
transient `schema` module).

STRUCTURED OUTPUT (LangChain pilot): by default the agent asks Gemini through
LangChain's `with_structured_output(TestCaseList)` so the response is a
schema-validated Pydantic object; it is then serialised straight back to a JSON
*string* so the return type and the TestCaseService contract are unchanged.
Constructing the agent with `structured=False` restores the original free-form
`_invoke()` path verbatim (kept for reversibility and debugging). Persistence is
unaffected — the Pydantic object is transient and is never stored.
"""

import json
import random
import time
from pathlib import Path

from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import ValidationError

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.business_analyst.prompt_manager import PromptManager
from app.agents.test_case.schema import TestCaseList
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# --- transient-failure retry for the Gemini structured call ------------------
# Gemini occasionally returns "503 UNAVAILABLE" / "model overloaded" / rate-limit
# errors under load. Those are worth a couple of quick retries; schema/validation
# or bad-request errors are not. Kept deliberately small: 3 attempts total, a
# few seconds of backoff at most, stdlib only (no new dependency).
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


class TestCaseAgentError(Exception):
    """Raised when the Gemini call fails or returns an unusable response."""

    __test__ = False  # not a pytest test class despite the "Test" prefix


class TestCaseAgent:
    """Wraps Gemini (via LangChain) to generate and refine test cases (JSON output)."""

    __test__ = False  # not a pytest test class despite the "Test" prefix

    def __init__(
        self,
        prompt_manager: PromptManager | None = None,
        *,
        structured: bool = True,
    ):
        self._prompt_manager = prompt_manager or PromptManager(prompts_dir=_PROMPTS_DIR)
        self._llm = ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            temperature=settings.gemini_temperature,
            google_api_key=settings.google_api_key,
        )
        # Structured output is the default path: Gemini is asked (via LangChain)
        # to return a value conforming to TestCaseList, so schema violations are
        # rejected at the LLM boundary. `structured=False` falls back to the
        # original free-form `_invoke()` string path unchanged.
        self._structured = structured
        self._structured_llm = (
            self._llm.with_structured_output(TestCaseList) if structured else None
        )

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
        try:
            response = self._llm.invoke(prompt)
        except Exception as exc:
            logger.error(f"Gemini API call failed: {exc}")
            raise TestCaseAgentError(f"Gemini API call failed: {exc}") from exc

        raw_content = getattr(response, "content", None)
        text = self._extract_text(raw_content).strip()

        if not text:
            logger.error("Gemini returned an empty response")
            raise TestCaseAgentError("Gemini returned an empty response")

        return text

    def _invoke_structured(self, prompt: str) -> TestCaseList:
        """Invoke the structured LLM and return a schema-validated TestCaseList.

        Transient Gemini failures (503 UNAVAILABLE / overloaded / rate-limit) are
        retried up to `_RETRY_MAX_ATTEMPTS` times with exponential backoff + jitter.
        Non-transient errors (schema/validation, bad request, auth) are not
        retried. On final failure — and for the post-response checks below (no
        result / empty `test_cases`, which are never retried) — the same
        `TestCaseAgentError` contract as before is preserved.
        """
        result = None
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                result = self._structured_llm.invoke(prompt)
                break
            except Exception as exc:
                if attempt < _RETRY_MAX_ATTEMPTS and _is_transient_llm_error(exc):
                    delay = _retry_backoff_seconds(attempt)
                    logger.warning(
                        "Gemini structured call failed (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        attempt, _RETRY_MAX_ATTEMPTS, delay, exc,
                    )
                    time.sleep(delay)
                    continue
                logger.error(f"Gemini structured call failed: {exc}")
                raise TestCaseAgentError(
                    f"Gemini structured call failed: {exc}"
                ) from exc

        if result is None:
            logger.error("Gemini returned no structured result")
            raise TestCaseAgentError("Gemini returned no structured result")
        if not isinstance(result, TestCaseList) or not result.test_cases:
            logger.error("Gemini structured result contained no test cases")
            raise TestCaseAgentError("Gemini structured result contained no test cases")

        return result

    def _run(self, prompt: str) -> str:
        """Produce the JSON string the service consumes, via the active path.

        Structured path: model -> TestCaseList -> `json.dumps(model_dump())`.
        Fallback path: the original free-form `_invoke()` string, untouched.
        Either way the return type is a JSON string.
        """
        if self._structured:
            return json.dumps(
                self._invoke_structured(prompt).model_dump(), ensure_ascii=False
            )
        return self._invoke(prompt)

    def generate_test_cases(
        self,
        brd_text: str,
        hld_text: str,
        lld_text: str,
        user_stories_text: str,
        metadata: ProjectMetadata,
    ) -> str:
        """Generate a full test-case set (JSON string) from the available artifacts.

        `hld_text` / `lld_text` / `user_stories_text` may be sentinel strings such
        as "(no accepted HLD available)" — the prompt handles those.
        """
        if not brd_text or not brd_text.strip():
            raise ValueError("Cannot generate test cases from empty BRD text")

        prompt = self._prompt_manager.render(
            "generate_test_cases.txt",
            brd_text=brd_text,
            hld_text=hld_text,
            lld_text=lld_text,
            user_stories_text=user_stories_text,
            project_name=metadata.project_name,
            client_name=metadata.client_name,
            project_type=metadata.project_type,
            industry=metadata.industry,
            language=metadata.language,
        )

        logger.info(f"Generating test cases for project '{metadata.project_name}'")
        result = self._run(prompt)
        logger.info(f"Test cases generated ({len(result)} chars of JSON)")
        return result

    def refine_test_cases(
        self,
        current_test_cases: str,
        user_feedback: str,
        current_version: int,
        brd_text: str,
        hld_text: str,
        lld_text: str,
        user_stories_text: str,
        metadata: ProjectMetadata,
    ) -> str:
        """Apply reviewer feedback to an existing test-case set. Returns a JSON string."""
        if not current_test_cases or not current_test_cases.strip():
            raise ValueError("Cannot refine an empty test-case set")
        if not user_feedback or not user_feedback.strip():
            raise ValueError("Refinement feedback cannot be empty")

        prompt = self._prompt_manager.render(
            "refine_test_cases.txt",
            current_test_cases=current_test_cases,
            user_feedback=user_feedback,
            current_version=str(current_version),
            brd_text=brd_text,
            hld_text=hld_text,
            lld_text=lld_text,
            user_stories_text=user_stories_text,
            project_name=metadata.project_name,
            client_name=metadata.client_name,
            project_type=metadata.project_type,
            industry=metadata.industry,
            language=metadata.language,
        )

        logger.info(
            f"Refining test cases from v{current_version} with feedback: "
            f"'{user_feedback[:80]}...'"
        )
        result = self._run(prompt)
        logger.info(f"Test cases refined ({len(result)} chars of JSON)")
        return result
