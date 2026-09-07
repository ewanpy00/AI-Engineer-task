# Образ для Railway. Питон и зависимости берутся ровно те же, что локально:
# версия из pyproject (>=3.12,<3.13), пакеты — из uv.lock, без резолва на билде.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Отдельный слой: зависимости меняются реже кода, кеш билда переживает правки app/.
# --no-install-project: сам пакет в venv не нужен, приложение запускается из /app.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY . .

# Каталог для JSONL-логов LLM. Без volume (OQ-3) он живёт до редеплоя, но
# существовать обязан: логгер (T-29) пишет в него, а git пустые каталоги не хранит.
#
# Владелец кода и venv — root, приложению отдан на запись только logs/: процесс
# под app не имеет права переписать собственный код, поэтому исполнение чужого
# кода внутри контейнера не превращается в постоянную закладку в образе.
RUN mkdir -p logs/llm \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app/logs
USER app

EXPOSE 8000

# Railway подставляет $PORT; 8000 — для локального `docker run` без него.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
