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
    gemini_model: str = "gemini-3.6-flash"
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
    # `gemini_max_retries` is passed to the SDK as `HttpRetryOptions(attempts=N)`
    # — N is the TOTAL number of HTTP requests per agent attempt, not the number
    # of retries (verified against langchain-google-genai 4.2.7 / google-genai
    # 2.8.0: 0 and 1 both mean a single request). The SDK's own retry ignores
    # Google's requested retry delay on 429 and retries after ~1 s, and it sits
    # UNDER every agent's application-level loop, so the two multiplied (the old
    # default 2 x 3 agent attempts = up to 6 requests per click — more than the
    # whole free-tier per-minute quota). Free-tier stabilisation: the default is
    # now 1 (SDK makes one request; the application policy in
    # `app/utils/gemini_retry.py` is the single, server-aware retry layer).
    gemini_timeout_seconds: int = 900
    gemini_max_retries: int = 1

    # Free-tier stabilisation — the application-level retry budget, shared by
    # all seven agents via `app.utils.gemini_retry.GeminiRetryPolicy`.
    #
    # `gemini_retry_max_attempts`: total attempts per Gemini call (1 = never
    # retry). Kept small; 503s get exponential backoff with jitter.
    # `gemini_retry_max_wait_seconds`: the most ONE call may sleep in total
    # between attempts. A 429 is retried (once) only when Google's requested
    # delay fits in this budget; otherwise the user immediately gets a clear
    # "wait N seconds" message instead of a Streamlit request blocking for
    # minutes. 0 disables all waiting (fail fast).
    gemini_retry_max_attempts: int = 3
    gemini_retry_max_wait_seconds: int = 60

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

    @field_validator("gemini_model")
    @classmethod
    def _validate_gemini_model(cls, value: str) -> str:
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError(
                "GEMINI_MODEL must not be empty (e.g. GEMINI_MODEL=gemini-3.6-flash)."
            )
        if any(ch.isspace() for ch in stripped):
            raise ValueError("GEMINI_MODEL must not contain whitespace.")
        return stripped  # store the trimmed value

    @field_validator("gemini_retry_max_attempts")
    @classmethod
    def _validate_gemini_retry_max_attempts(cls, value: int) -> int:
        if not (1 <= value <= 5):
            raise ValueError("GEMINI_RETRY_MAX_ATTEMPTS must be an integer from 1 to 5.")
        return value

    @field_validator("gemini_retry_max_wait_seconds")
    @classmethod
    def _validate_gemini_retry_max_wait_seconds(cls, value: int) -> int:
        if not (0 <= value <= 300):
            raise ValueError(
                "GEMINI_RETRY_MAX_WAIT_SECONDS must be an integer from 0 to 300."
            )
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
            "retry_max_attempts": self.gemini_retry_max_attempts,
            "retry_max_wait_seconds": self.gemini_retry_max_wait_seconds,
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
