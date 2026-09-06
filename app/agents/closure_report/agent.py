"""
Closure Report Agent (Phase 7).

An END-OF-SDLC synthesis agent. It receives a fully-prepared, deterministic
evidence bundle from `ClosureReportService` and asks Gemini for NARRATIVE PROSE
ONLY - executive summary, scope summary, findings interpretation, outstanding
items explanation, closure narrative, limitations. It never calculates a count,
a coverage figure, a version, a finalization state, or the closure status; those
are decided by the service before this agent is called and passed in as fixed
inputs.

Built structurally parallel to the earlier agents (BusinessAnalystAgent /
SolutionArchitectAgent / InitialUserStoryAgent / LowLevelDesignAgent /
UserStoryRefinementAgent / TestCaseAgent) rather than sharing a base class.
`_extract_text` / `_invoke` and the transient-retry block are intentionally
duplicated from `app/agents/test_case/agent.py` - the deferred consolidation
cleanup described in CLAUDE.md.

This module imports no other agent package's implementation - only the shared
`ProjectMetadata` value type, `PromptManager` infrastructure, and this package's
own transient `schema` module.
"""

import json
import random
import time
from pathlib import Path

from pydantic import ValidationError

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.business_analyst.prompt_manager import PromptManager
from app.agents.closure_report.schema import ClosureNarrative
from app.utils.llm import build_chat_llm
from app.utils.logger import get_logger

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# --- transient-failure retry for the Gemini structured call ------------------
# Gemini occasionally returns "503 UNAVAILABLE" / "model overloaded" / rate-limit
# errors under load. Those are worth a couple of quick retries; schema/validation
# or bad-request errors are not. Kept deliberately small: 3 attempts total, a
# few seconds of backoff at most, stdlib only (no new dependency). Same policy as
# app/agents/test_case/agent.py.
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
    # Phase 11A — transient transport-level failures observed in the Phase 11
    # benchmark (a mid-response server disconnect during closure narrative
    # synthesis aborted the whole pipeline because it was NOT retryable).
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


class ClosureReportAgentError(Exception):
    """Raised when the Gemini call fails or returns an unusable response."""


class ClosureReportAgent:
    """Wraps Gemini (via LangChain) to produce a closure-report NARRATIVE (JSON string)."""

    def __init__(
        self,
        prompt_manager: PromptManager | None = None,
        *,
        structured: bool = True,
    ):
        self._prompt_manager = prompt_manager or PromptManager(prompts_dir=_PROMPTS_DIR)
        # Structured output is the default path: Gemini is asked (via LangChain)
        # to return a value conforming to ClosureNarrative, so schema violations
        # are rejected at the LLM boundary. `structured=False` falls back to the
        # free-form `_invoke()` string path unchanged.
        self._structured = structured
        # Phase 11A: base client AND structured wrapper are built lazily on the
        # first real synthesize call — see `app/utils/llm.py`. A test may inject
        # a fake by assigning `agent._structured_llm` (or `agent._llm`) first.
        self._llm = None
        self._structured_llm = None

    def _ensure_llm(self):
        """Build the base Gemini client on first use, then reuse it."""
        if self._llm is None:
            self._llm = build_chat_llm()
        return self._llm

    def _ensure_structured_llm(self):
        """The structured-output wrapper, or None when `structured=False`.

        Built lazily; a pre-assigned `self._structured_llm` (tests inject one)
        is used as-is.
        """
        if not self._structured:
            return None
        if self._structured_llm is None:
            self._structured_llm = self._ensure_llm().with_structured_output(
                ClosureNarrative
            )
        return self._structured_llm

    @staticmethod
    def _extract_text(content) -> str:
        """Extract plain text from an LLM response's `.content`.

        Newer versions of langchain-google-genai (and Gemini 3+ models) return
        `.content` as a dict or list of structured content blocks - e.g.
        {"type": "text", "text": "...", "extras": {...}} - rather than a plain
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
            response = self._ensure_llm().invoke(prompt)
        except Exception as exc:
            logger.error(f"Gemini API call failed: {exc}")
            raise ClosureReportAgentError(f"Gemini API call failed: {exc}") from exc

        raw_content = getattr(response, "content", None)
        text = self._extract_text(raw_content).strip()

        if not text:
            logger.error("Gemini returned an empty response")
            raise ClosureReportAgentError("Gemini returned an empty response")

        return text

    def _invoke_structured(self, prompt: str) -> ClosureNarrative:
        """Invoke the structured LLM and return a schema-validated ClosureNarrative.

        Transient Gemini failures (503 UNAVAILABLE / overloaded / rate-limit) are
        retried up to `_RETRY_MAX_ATTEMPTS` times with exponential backoff + jitter.
        Non-transient errors (schema/validation, bad request, auth) are not
        retried. On final failure - and for the post-response checks below (no
        result) - the same `ClosureReportAgentError` contract is preserved.
        """
        structured_llm = self._ensure_structured_llm()
        result = None
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                result = structured_llm.invoke(prompt)
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
                raise ClosureReportAgentError(
                    f"Gemini structured call failed: {exc}"
                ) from exc

        if result is None:
            logger.error("Gemini returned no structured result")
            raise ClosureReportAgentError("Gemini returned no structured result")
        if not isinstance(result, ClosureNarrative):
            logger.error("Gemini structured result was not a ClosureNarrative")
            raise ClosureReportAgentError(
                "Gemini structured result was not a ClosureNarrative"
            )

        return result

    def _run(self, prompt: str) -> str:
        """Produce the JSON string the service consumes, via the active path.

        Structured path: model -> ClosureNarrative -> `json.dumps(model_dump())`.
        Fallback path: the original free-form `_invoke()` string, untouched.
        Either way the return type is a JSON string.
        """
        if self._structured:
            return json.dumps(
                self._invoke_structured(prompt).model_dump(), ensure_ascii=False
            )
        return self._invoke(prompt)

    def synthesize_narrative(
        self,
        evidence_json: str,
        closure_status: str,
        metadata: ProjectMetadata,
    ) -> str:
        """Return a closure-report narrative (JSON string) for the supplied evidence.

        `evidence_json` is the deterministic evidence bundle assembled by
        `ClosureReportService` (already JSON-serialised). `closure_status` is the
        status the SERVICE has already decided - it is passed in only so the
        narrative can be phrased consistently; the prompt forbids the model from
        changing it. This agent computes nothing.
        """
        if not evidence_json or not evidence_json.strip():
            raise ValueError("Cannot synthesize a closure report from empty evidence")
        if not closure_status or not closure_status.strip():
            raise ValueError("Closure status must be supplied by the service")

        prompt = self._prompt_manager.render(
            "closure_report.txt",
            evidence_json=evidence_json,
            closure_status=closure_status,
            project_name=metadata.project_name,
            client_name=metadata.client_name,
            project_type=metadata.project_type,
            industry=metadata.industry,
            language=metadata.language,
        )

        logger.info(
            f"Synthesizing closure report narrative for project "
            f"'{metadata.project_name}' (status={closure_status})"
        )
        result = self._run(prompt)
        logger.info(f"Closure report narrative generated ({len(result)} chars of JSON)")
        return result
