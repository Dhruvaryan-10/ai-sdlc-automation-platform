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

from dataclasses import dataclass
from datetime import date

from langchain_google_genai import ChatGoogleGenerativeAI

from app.agents.business_analyst.prompt_manager import PromptManager
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


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

    def __init__(self, prompt_manager: PromptManager | None = None):
        self._prompt_manager = prompt_manager or PromptManager()
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