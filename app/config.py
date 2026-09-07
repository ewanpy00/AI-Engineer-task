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
    # Пустая строка по умолчанию — fail-closed: незаданный ADMIN_TOKEN закрывает
    # админку целиком (`require_admin` отвергает всё), а не открывает её на
    # угадываемом значении. Незаполненная переменная не должна выглядеть как
    # рабочая конфигурация.
    admin_token: str = ""
    # выключается на время локальных прогонов и тестов, чтобы плановый заход
    # не стартовал посреди ручного (T-48)
    scheduler_enabled: bool = True
    google_api_key: str = ""
    ya300_session_id: str = ""

    # Догоняющее обновление: сколько игр без обложки или без Metascore заход
    # добирает из своей базы сверх фазы дня. Общий размер батча от этого не
    # растёт — добор занимает место, которое не занял дневной курсор. `0`
    # выключает добор целиком.
    catchup_limit: int = 5

    # LLM (11-decisions.md OQ-1: Google AI Studio вместо Anthropic из design §5).
    # Модель проверена на живом API (00-research.md, приложение от 2026-09-07):
    # 8/8 вызовов подряд с валидным structured output, ~1.2-2.0 с на вызов, ни
    # одного 429. Квота бесплатного тира считается отдельно по каждой модели,
    # так что смена модели — это и смена квоты. Меняется имя здесь или через
    # env GEMINI_MODEL, архитектура адаптера от него не зависит.
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_timeout_s: float = 60.0
    gemini_max_retries: int = 3       # design §5.5: 3 попытки, backoff 2/4/8 с
    gemini_failure_limit: int = 5     # design §5.5: circuit breaker на заход
    # Бесплатный тир AI Studio отдаёт 429 уже на нескольких запросах подряд
    # (проверено на живом API). `Semaphore(4)` из T-22 умножился бы на две
    # аудитории, поэтому обращения к модели троттлятся отдельно от Metacritic.
    gemini_max_concurrency: int = 2
    llm_enabled: bool = True          # рубильник для тестов и прогонов без ключа

    # Летсплеи (доп. часть 1, ADR-8). Фича best-effort целиком: без
    # YA300_SESSION_ID пайплайн не отключается молча, а пишет статус `disabled`,
    # чтобы в базе было видно, почему пересказов нет.
    letsplay_enabled: bool = True
    # ytsearch отдаёт выдачу разом, из неё выбирается самый просматриваемый:
    # десяти результатов хватает, чтобы мимо не прошёл заметный ролик.
    letsplay_search_results: int = 10
    # Отсекает трейлеры и шортсы: просмотров у них бывает больше, чем у любого
    # летсплея, а рассказа блогера в них нет. Если под фильтр не попал никто,
    # берётся самый просматриваемый из всей выдачи (см. finder).
    letsplay_min_duration_s: int = 300
    letsplay_search_timeout_s: float = 30.0
    # yt-dlp синхронный и ходит в сеть из отдельного потока, 300.ya.ru держит
    # запрос до конца генерации: обе операции идут мимо лимитера Metacritic,
    # поэтому у них свой потолок параллелизма.
    letsplay_max_concurrency: int = 2
    ya300_base_url: str = "https://300.ya.ru"
    ya300_timeout_s: float = 60.0     # T-42: одна попытка, дольше не ждём

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
    prompts_dir: Path = BASE_DIR / "prompts"
    llm_log_dir: Path = BASE_DIR / "logs" / "llm"

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
