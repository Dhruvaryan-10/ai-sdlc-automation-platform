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

from pathlib import Path

from langchain_google_genai import ChatGoogleGenerativeAI

# Reuse the BA agent's metadata container rather than defining a parallel one.
from app.agents.business_analyst.agent import ProjectMetadata
from app.agents.business_analyst.prompt_manager import PromptManager
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


class UserStoryRefinementAgentError(Exception):
    """Raised when the Gemini call fails or returns an unusable response."""


class UserStoryRefinementAgent:
    """Wraps Gemini (via LangChain) to reconcile user stories against project artifacts."""

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
