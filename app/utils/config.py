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
