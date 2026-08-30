"""
Initial User Story Agent (Phase 3).

Built structurally parallel to BusinessAnalystAgent / SolutionArchitectAgent —
same LangChain/Gemini wrapper pattern, same external-prompt approach, same
response normalization — rather than sharing an abstract base class. Consolidating
the duplicated `_extract_text` / `_invoke` across the three agents is a deliberate
deferred cleanup, not part of Phase 3.

This class has exactly two responsibilities:
    1. generate_stories()  -> first-draft user stories from an accepted/final BRD
    2. refine_stories()    -> apply targeted feedback to an existing set

It does NOT know about the final-BRD gate, versioning, or the UI — that lives in
InitialUserStoryService. It has NO dependency on the Solution Architect Agent.
"""

from datetime import date
from pathlib import Path

from langchain_google_genai import ChatGoogleGenerativeAI

# Reuse the BA agent's metadata container rather than defining a parallel one.
from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.business_analyst.prompt_manager import PromptManager
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


class InitialUserStoryAgentError(Exception):
    """Raised when the Gemini call fails or returns an unusable response."""


class InitialUserStoryAgent:
    """Wraps Gemini (via LangChain) to generate and refine draft user stories."""

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
            raise InitialUserStoryAgentError(f"Gemini API call failed: {exc}") from exc

        raw_content = getattr(response, "content", None)
        text = self._extract_text(raw_content).strip()

        if not text:
            logger.error("Gemini returned an empty response")
            raise InitialUserStoryAgentError("Gemini returned an empty response")

        return text

    def generate_stories(self, brd_text: str, metadata: ProjectMetadata) -> str:
        """Generate draft user stories (Version 1) from an accepted/final BRD."""
        if not brd_text or not brd_text.strip():
            raise ValueError("Cannot generate user stories from empty BRD text")

        prompt = self._prompt_manager.render(
            "generate_user_stories.txt",
            brd_text=brd_text,
            project_name=metadata.project_name,
            client_name=metadata.client_name,
            project_type=metadata.project_type,
            industry=metadata.industry,
            language=metadata.language,
            output_format=metadata.output_format,
            generated_date=date.today().isoformat(),
        )

        logger.info(f"Generating draft user stories v1 for project '{metadata.project_name}'")
        stories_text = self._invoke(prompt)
        logger.info(f"Draft user stories v1 generated ({len(stories_text)} chars)")
        return stories_text

    def refine_stories(self, current_stories: str, user_feedback: str, current_version: int) -> str:
        """Apply targeted feedback to an existing set of stories and return the full document."""
        if not current_stories or not current_stories.strip():
            raise ValueError("Cannot refine an empty set of user stories")
        if not user_feedback or not user_feedback.strip():
            raise ValueError("Refinement feedback cannot be empty")

        prompt = self._prompt_manager.render(
            "refine_user_stories.txt",
            current_stories=current_stories,
            user_feedback=user_feedback,
            current_version=str(current_version),
        )

        logger.info(
            f"Refining user stories from v{current_version} with feedback: '{user_feedback[:80]}...'"
        )
        refined_text = self._invoke(prompt)
        logger.info(f"User stories refined ({len(refined_text)} chars)")
        return refined_text
