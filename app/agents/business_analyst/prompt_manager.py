"""
Prompt Manager.

WHY THIS EXISTS:
The spec explicitly forbids hardcoding prompts inside Python. Prompts are
business logic that a Business Analyst / prompt engineer should be able to
tune without touching code or redeploying the app. This class's only job is:
    1. Load a .txt template file from disk.
    2. Substitute named placeholders like {project_name}.
    3. Fail loudly (not silently) if a required placeholder is missing.

Using Python's built-in str.format_map with a safe dict means we get clear
KeyError-free behavior: if the template references a placeholder we didn't
supply, we raise a descriptive error instead of a cryptic KeyError deep in
string formatting.
"""

import string
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


class PromptRenderError(Exception):
    """Raised when a prompt template references a placeholder that wasn't provided."""


class _SafeFormatter(string.Formatter):
    """A string.Formatter that raises a clear error for missing keys instead of KeyError."""

    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            if key not in kwargs:
                raise PromptRenderError(
                    f"Prompt template references placeholder '{{{key}}}' "
                    f"which was not supplied to PromptManager.render()."
                )
            return kwargs[key]
        return super().get_value(key, args, kwargs)


class PromptManager:
    """Loads prompt templates by name and renders them with placeholder values."""

    def __init__(self, prompts_dir: Path = _PROMPTS_DIR):
        self._prompts_dir = prompts_dir
        self._formatter = _SafeFormatter()

    def _load_template(self, template_name: str) -> str:
        template_path = self._prompts_dir / template_name
        if not template_path.exists():
            raise FileNotFoundError(f"Prompt template not found: {template_path}")
        return template_path.read_text(encoding="utf-8")

    def render(self, template_name: str, **placeholders: str) -> str:
        """Render a named template (e.g. 'generate_brd.txt') with the given placeholders."""
        template_text = self._load_template(template_name)
        try:
            rendered = self._formatter.format(template_text, **placeholders)
        except PromptRenderError:
            logger.error(f"Missing placeholder while rendering '{template_name}'")
            raise
        logger.info(f"Rendered prompt template '{template_name}' ({len(rendered)} chars)")
        return rendered
