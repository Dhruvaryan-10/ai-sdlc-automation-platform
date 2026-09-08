"""
Centralized application configuration.

WHY THIS FILE EXISTS:
Hardcoding API keys, model names, or folder paths inside business logic makes
the app impossible to reconfigure per environment (dev/test/prod) without
touching code. Pydantic's BaseSettings reads values from environment
variables / a .env file and validates them once, at startup, so every other
module just imports `settings` instead of calling os.getenv() everywhere.

Phase 13B — configuration hardening: every field is now range/vocabulary
validated so an invalid `.env` fails FAST with a clear `pydantic.ValidationError`
at `Settings()` time, instead of surfacing much later (a `400` from Gemini, or a
`ValueError` deep inside `logging`). Field names, environment-variable names,
defaults, and the singleton architecture are unchanged. Directory helpers now
return ABSOLUTE paths (same effective location for the documented "launch from
the repository root" workflow) so downstream code is not sensitive to a later
`chdir`. `summary_for_log()` gives a secret-free view of the effective config
for a single sanitized startup log line.
"""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_API_KEY_PLACEHOLDER = "your_gemini_api_key_here"


def _abs_dir(raw: str) -> Path:
    """Absolute representation of a configured directory. Non-existent paths are
    fine (`resolve()` does not require existence); a value that is already
    absolute is returned unchanged."""
    return Path(raw).resolve()


class Settings(BaseSettings):
    # --- Gemini / LLM configuration ---
    google_api_key: str
    gemini_model: str = "gemini-3.5-flash"
    gemini_temperature: float = 0.3

    # Phase 11A — explicit, configurable timeout / retry policy for the Gemini
    # client (previously implicit SDK defaults: no client-side timeout at all,
    # and max_retries=6).
    #
    # `gemini_timeout_seconds` bounds a *hung* connection. It is NOT meant to
    # abort a slow-but-live generation: Phase 11 reconnaissance measured a
    # legitimate LLD generation at ~426 s, so the default (900 s) is ~2x that
    # worst observed case. Raise `GEMINI_TIMEOUT_SECONDS` for slow links or
    # unusually large documents.
    #
    # `gemini_max_retries` is the SDK-level HTTP retry budget. The structured
    # agents (test_case, closure_report) additionally run their own bounded
    # 3-attempt application-level loop with jittered backoff; keeping the SDK
    # budget small (default 2) stops the two from multiplying (was up to
    # 6 x 3 = 18 attempts; now at most 2 x 3 = 6). Set `GEMINI_MAX_RETRIES=0`
    # to make the application loop the sole retry mechanism.
    gemini_timeout_seconds: int = 900
    gemini_max_retries: int = 2

    # --- Folder configuration ---
    upload_dir: str = "uploads"
    output_dir: str = "outputs"
    log_dir: str = "logs"

    # --- Logging ---
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Phase 13B validators (deterministic; reject-only; never echo secrets) ---

    @field_validator("google_api_key")
    @classmethod
    def _validate_google_api_key(cls, value: str) -> str:
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError(
                "GOOGLE_API_KEY is required and must not be empty. Set it in your "
                ".env file (see .env.example)."
            )
        if stripped == _API_KEY_PLACEHOLDER:
            raise ValueError(
                "GOOGLE_API_KEY is still the .env.example placeholder — replace it "
                "with your real Gemini API key."
            )
        return stripped  # store the trimmed value

    @field_validator("gemini_temperature")
    @classmethod
    def _validate_gemini_temperature(cls, value: float) -> float:
        if not (0.0 <= value <= 2.0):
            raise ValueError(
                "GEMINI_TEMPERATURE must be between 0.0 and 2.0 inclusive."
            )
        return value

    @field_validator("gemini_timeout_seconds")
    @classmethod
    def _validate_gemini_timeout_seconds(cls, value: int) -> int:
        if value < 1:
            raise ValueError("GEMINI_TIMEOUT_SECONDS must be an integer >= 1.")
        return value

    @field_validator("gemini_max_retries")
    @classmethod
    def _validate_gemini_max_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError("GEMINI_MAX_RETRIES must be an integer >= 0.")
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = (value or "").strip().upper()
        if normalized not in _VALID_LOG_LEVELS:
            raise ValueError(
                "LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL "
                "(case-insensitive)."
            )
        return normalized  # store normalized (upper-case)

    # --- directory helpers (absolute; create on demand) ---

    def resolved_upload_dir(self) -> Path:
        path = _abs_dir(self.upload_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolved_output_dir(self) -> Path:
        path = _abs_dir(self.output_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolved_log_dir(self) -> Path:
        path = _abs_dir(self.log_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- safe, log-friendly view of the effective configuration ---

    def summary_for_log(self) -> dict:
        """Only non-secret configuration metadata, for a single startup log line.

        NEVER contains `google_api_key`, the environment, or any secret. No
        filesystem side effects (directories are resolved but not created here).
        """
        return {
            "model": self.gemini_model,
            "temperature": self.gemini_temperature,
            "timeout_seconds": self.gemini_timeout_seconds,
            "max_retries": self.gemini_max_retries,
            "log_level": self.log_level,
            "output_dir": str(_abs_dir(self.output_dir)),
            "upload_dir": str(_abs_dir(self.upload_dir)),
            "log_dir": str(_abs_dir(self.log_dir)),
        }


# Singleton instance imported by every other module.
# Raises a clear pydantic ValidationError at startup if GOOGLE_API_KEY is missing
# or any setting is out of range, instead of failing confusingly later. The
# Streamlit startup guard (app/ui/streamlit_app.py) turns that ValidationError
# into a clean user-facing message.
settings = Settings()
