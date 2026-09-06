"""DATABASE_URL из Railway приходит в libpq-форме — проверяем приведение (T-11)."""

from app.config import normalize_database_url


def test_railway_postgres_url_gets_async_driver():
    url = normalize_database_url("postgresql://user:pw@postgres.railway.internal:5432/railway")
    assert url == "postgresql+asyncpg://user:pw@postgres.railway.internal:5432/railway"


def test_heroku_style_postgres_scheme_also_normalized():
    assert normalize_database_url("postgres://u:p@h:5432/db").startswith("postgresql+asyncpg://")


def test_explicit_asyncpg_url_untouched():
    url = "postgresql+asyncpg://u:p@h:5432/db"
    assert normalize_database_url(url) == url


def test_sslmode_translated_to_asyncpg_ssl():
    url = normalize_database_url("postgresql://u:p@h:5432/db?sslmode=require")
    assert url == "postgresql+asyncpg://u:p@h:5432/db?ssl=require"


def test_sslmode_disable_dropped_without_ssl_param():
    url = normalize_database_url("postgresql://u:p@h:5432/db?sslmode=disable")
    assert url == "postgresql+asyncpg://u:p@h:5432/db"
