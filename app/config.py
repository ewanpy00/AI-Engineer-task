"""Единая точка чтения конфига из переменных окружения."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def normalize_database_url(url: str) -> str:
    """Приводит DATABASE_URL к виду, который понимает async-движок.

    Railway отдаёт переменную Postgres-аддона в libpq-форме `postgresql://…`:
    SQLAlchemy выбрал бы по ней синхронный psycopg2, которого в образе нет.
    Параметр `sslmode` — тоже из libpq, asyncpg его не знает и падает на
    `connect() got an unexpected keyword argument`; TLS ему задаётся как `ssl`.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix) :]
            break

    split = urlsplit(url)
    params = parse_qsl(split.query, keep_blank_values=True)
    if not any(key == "sslmode" for key, _ in params):
        return url

    kept = [(key, value) for key, value in params if key != "sslmode"]
    sslmode = next(value for key, value in params if key == "sslmode")
    if sslmode != "disable" and not any(key == "ssl" for key, _ in kept):
        kept.append(("ssl", "require" if sslmode != "allow" else "prefer"))
    return urlunsplit(split._replace(query=urlencode(kept)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str
    admin_token: str = "dev"
    # выключается на время локальных прогонов и тестов, чтобы плановый заход
    # не стартовал посреди ручного (T-48)
    scheduler_enabled: bool = True
    google_api_key: str = ""
    ya300_session_id: str = ""

    metacritic_base_url: str = "https://backend.metacritic.com"
    # research зафиксировал .../a/img/{bucketPath} — сегодня это 404: между /a/img
    # и bucketPath обязателен bucketType, у игровых обложек он всегда "catalog"
    metacritic_cdn_url: str = "https://www.metacritic.com/a/img/catalog"
    metacritic_user_agent: str = "metacritic-digest/0.1 (+contact: local dev)"
    metacritic_rps: float = 1.0
    metacritic_max_retries: int = 3
    metacritic_timeout_s: float = 20.0

    schema_path: Path = BASE_DIR / "schema.sql"
    templates_dir: Path = BASE_DIR / "templates"

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
