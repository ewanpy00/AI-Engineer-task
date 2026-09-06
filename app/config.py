"""Единая точка чтения конфига из переменных окружения."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str
    admin_token: str = "dev"
    google_api_key: str = ""
    ya300_session_id: str = ""

    metacritic_base_url: str = "https://backend.metacritic.com"
    metacritic_cdn_url: str = "https://www.metacritic.com/a/img"
    metacritic_user_agent: str = "metacritic-digest/0.1 (+contact: local dev)"
    metacritic_rps: float = 1.0
    metacritic_max_retries: int = 3
    metacritic_timeout_s: float = 20.0

    schema_path: Path = BASE_DIR / "schema.sql"
    templates_dir: Path = BASE_DIR / "templates"


@lru_cache
def get_settings() -> Settings:
    return Settings()
