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
ProjectMetadata value type and PromptManager infrastructure).
"""

from pathlib import Path

from langchain_google_genai import ChatGoogleGenerativeAI

from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.business_analyst.prompt_manager import PromptManager
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


class TestCaseAgentError(Exception):
    """Raised when the Gemini call fails or returns an unusable response."""

    __test__ = False  # not a pytest test class despite the "Test" prefix


class TestCaseAgent:
    """Wraps Gemini (via LangChain) to generate and refine test cases (JSON output)."""

    __test__ = False  # not a pytest test class despite the "Test" prefix

    def __init__(self, prompt_manager: PromptManager | None = None):
        self._prompt_manager = prompt_manager or PromptManager(prompts_dir=_PROMPTS_DIR)
        self._llm = ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            temperature=settings.gemini_temperature,
            google_api_key=settings.google_api_key,
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
        result = self._invoke(prompt)
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
        result = self._invoke(prompt)
        logger.info(f"Test cases refined ({len(result)} chars of JSON)")
        return result
