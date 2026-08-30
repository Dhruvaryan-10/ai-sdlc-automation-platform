"""
Low-Level Design Agent (Phase 4).

Built structurally parallel to the earlier agents (BusinessAnalystAgent /
SolutionArchitectAgent / InitialUserStoryAgent) — same LangChain/Gemini wrapper
pattern, same external-prompt approach, same response normalization — rather than
sharing an abstract base class. Consolidating the duplicated `_extract_text` /
`_invoke` across the four agents is a deliberate deferred cleanup, not part of
Phase 4.

This class has exactly two responsibilities:
    1. generate_lld()  -> first-draft LLD from an accepted HLD (+ BRD / optional
                          draft user-story context)
    2. refine_lld()    -> apply targeted feedback to an existing LLD

It does NOT know about the final-HLD gate, versioning, artifact loading, or the
UI — that lives in LowLevelDesignService. It has NO dependency on the Initial
User Story Agent.
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


class LLDAgentError(Exception):
    """Raised when the Gemini call fails or returns an unusable response."""


class LowLevelDesignAgent:
    """Wraps Gemini (via LangChain) to generate and refine Low-Level Designs."""

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
            raise LLDAgentError(f"Gemini API call failed: {exc}") from exc

        raw_content = getattr(response, "content", None)
        text = self._extract_text(raw_content).strip()

        if not text:
            logger.error("Gemini returned an empty response")
            raise LLDAgentError("Gemini returned an empty response")

        return text

    def generate_lld(
        self,
        hld_text: str,
        brd_text: str,
        user_stories_text: str,
        metadata: ProjectMetadata,
    ) -> str:
        """Generate an LLD (Version 1) from an accepted HLD plus supporting context.

        `brd_text` and `user_stories_text` may be sentinel strings such as
        "(no accepted BRD available)" / "(no draft user stories available)" —
        the prompt template is written to handle those.
        """
        if not hld_text or not hld_text.strip():
            raise ValueError("Cannot generate an LLD from empty HLD text")

        prompt = self._prompt_manager.render(
            "generate_lld.txt",
            hld_text=hld_text,
            brd_text=brd_text,
            user_stories_text=user_stories_text,
            project_name=metadata.project_name,
            client_name=metadata.client_name,
            project_type=metadata.project_type,
            industry=metadata.industry,
            language=metadata.language,
            output_format=metadata.output_format,
            generated_date=date.today().isoformat(),
        )

        logger.info(f"Generating LLD v1 for project '{metadata.project_name}'")
        lld_text = self._invoke(prompt)
        logger.info(f"LLD v1 generated ({len(lld_text)} chars)")
        return lld_text

    def refine_lld(self, current_lld: str, user_feedback: str, current_version: int) -> str:
        """Apply targeted feedback to an existing LLD and return the full updated document."""
        if not current_lld or not current_lld.strip():
            raise ValueError("Cannot refine an empty LLD")
        if not user_feedback or not user_feedback.strip():
            raise ValueError("Refinement feedback cannot be empty")

        prompt = self._prompt_manager.render(
            "refine_lld.txt",
            current_lld=current_lld,
            user_feedback=user_feedback,
            current_version=str(current_version),
        )

        logger.info(
            f"Refining LLD from v{current_version} with feedback: '{user_feedback[:80]}...'"
        )
        refined_text = self._invoke(prompt)
        logger.info(f"LLD refined ({len(refined_text)} chars)")
        return refined_text
