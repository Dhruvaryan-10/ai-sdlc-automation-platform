"""
User Story Refinement Agent (Phase 5).

Built structurally parallel to the earlier agents (BusinessAnalystAgent /
SolutionArchitectAgent / InitialUserStoryAgent / LowLevelDesignAgent) — same
LangChain/Gemini wrapper pattern, same external-prompt approach, same response
normalization — rather than sharing an abstract base class. Consolidating the
duplicated `_extract_text` / `_invoke` across the five agents is a deliberate
deferred cleanup, not part of Phase 5.

This class has exactly one responsibility:
    refine_user_stories() -> reconcile the current user stories against the
                             accepted BRD (primary) plus optional HLD / LLD
                             context, returning the full updated document.

It does NOT know about prerequisites, artifact loading, versioning, staleness,
or the UI — that lives in UserStoryRefinementService. It has NO dependency on
the Initial User Story, Solution Architect, or Low-Level Design agent packages.
"""

import random
import time

from pathlib import Path

# Reuse the BA agent's metadata container rather than defining a parallel one.
from pydantic import ValidationError

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.business_analyst.prompt_manager import PromptManager
from app.utils.llm import build_chat_llm
from app.utils.logger import get_logger
from app.utils.metrics import instrumented_invoke

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"

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


class UserStoryRefinementAgentError(Exception):
    """Raised when the Gemini call fails or returns an unusable response."""


class UserStoryRefinementAgent:
    """Wraps Gemini (via LangChain) to reconcile user stories against project artifacts."""

    _TELEMETRY_STAGE = "user_story_refinement"  # Phase 13A: SDLC stage tag for LLM telemetry

    def __init__(self, prompt_manager: PromptManager | None = None):
        self._prompt_manager = prompt_manager or PromptManager(prompts_dir=_PROMPTS_DIR)
        # Phase 11A: lazily built on first real refine call.
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
        request, auth) are surfaced immediately. The ``UserStoryRefinementAgentError`` contract -- and the
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
                raise UserStoryRefinementAgentError(f"Gemini API call failed: {exc}") from exc

        raw_content = getattr(response, "content", None)
        text = self._extract_text(raw_content).strip()

        if not text:
            logger.error("Gemini returned an empty response")
            raise UserStoryRefinementAgentError("Gemini returned an empty response")

        return text

    def refine_user_stories(
        self,
        current_stories: str,
        brd_text: str,
        hld_text: str,
        lld_text: str,
        metadata: ProjectMetadata,
        current_version: int = 1,
    ) -> str:
        """Reconcile `current_stories` against the BRD (+ optional HLD/LLD context).

        `hld_text` / `lld_text` may be sentinel strings such as
        "(no accepted HLD available)" / "(no accepted LLD available)" — the
        prompt template is written to handle those.
        """
        if not current_stories or not current_stories.strip():
            raise ValueError("Cannot refine an empty set of user stories")
        if not brd_text or not brd_text.strip():
            raise ValueError("Cannot refine user stories without BRD text")

        prompt = self._prompt_manager.render(
            "refine_from_artifacts.txt",
            current_stories=current_stories,
            brd_text=brd_text,
            hld_text=hld_text,
            lld_text=lld_text,
            project_name=metadata.project_name,
            client_name=metadata.client_name,
            project_type=metadata.project_type,
            industry=metadata.industry,
            language=metadata.language,
            output_format=metadata.output_format,
            current_version=str(current_version),
        )

        logger.info(
            f"Refining user stories from v{current_version} against project artifacts "
            f"for project '{metadata.project_name}'"
        )
        refined_text = self._invoke(prompt)
        logger.info(f"User stories refined from artifacts ({len(refined_text)} chars)")
        return refined_text
