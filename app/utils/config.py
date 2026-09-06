"""
Centralized application configuration.

WHY THIS FILE EXISTS:
Hardcoding API keys, model names, or folder paths inside business logic makes
the app impossible to reconfigure per environment (dev/test/prod) without
touching code. Pydantic's BaseSettings reads values from environment
variables / a .env file and validates them once, at startup, so every other
module just imports `settings` instead of calling os.getenv() everywhere.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    def resolved_upload_dir(self) -> Path:
        path = Path(self.upload_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolved_output_dir(self) -> Path:
        path = Path(self.output_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolved_log_dir(self) -> Path:
        path = Path(self.log_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path


# Singleton instance imported by every other module.
# Raises a clear pydantic ValidationError at startup if GOOGLE_API_KEY is missing,
# instead of failing confusingly deep inside an API call later.
settings = Settings()
