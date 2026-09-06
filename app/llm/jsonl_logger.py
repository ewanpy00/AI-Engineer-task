"""T-29: построчный лог всех обращений к LLM (CLAUDE.md, design §5.4).

Одна строка на попытку, пишется **после** ответа — и при успехе, и при отказе.
`messages` кладутся целиком, как отправлены: усечение цитат уже произошло
раньше, на сборке (T-30), и лог должен показывать ровно то, что ушло в модель.
Это же делает лог единственным способом проверить правило проекта —
что отзывы никогда не попадают в system-роль.

Отклонение от design §4.4: `write` асинхронный. Синхронная сигнатура из
контракта не даёт взять `asyncio.Lock`, а без него параллельные заходы
(`Semaphore(4)` из T-22) перемешали бы строки.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

# Порядок полей в строке фиксирован: глазами лог читают чаще, чем машиной,
# и `ts`/`prompt_version`/`status` должны быть в начале.
FIELD_ORDER = (
    "ts", "prompt_version", "prompt_sha256", "model", "game_id", "run_id",
    "audience", "attempt", "status", "latency_ms", "tokens", "error",
    "messages", "response",
)


def utc_now_iso() -> str:
    """`2026-09-06T12:03:11.204Z` — как в примере design §5.4."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonlLogger:
    """Append-only запись в `logs/llm/{YYYY-MM-DD}.jsonl`.

    Файл выбирается по дате записи, а не по дате старта захода: заход,
    начавшийся в 23:59, допишет остаток в файл следующих суток — это честнее,
    чем держать открытый файл сутками.
    """

    def __init__(self, directory: Path | None = None, settings: Settings | None = None) -> None:
        self._dir = directory or (settings or get_settings()).llm_log_dir
        self._lock = asyncio.Lock()

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, moment: datetime | None = None) -> Path:
        day = (moment or datetime.now(UTC)).astimezone(UTC).date()
        return self._dir / f"{day.isoformat()}.jsonl"

    async def write(self, record: dict[str, Any]) -> None:
        """Пишет одну строку. Наружу не бросает: лог не должен ронять обход.

        Строка собирается в память целиком и уходит одним `write` под локой —
        так параллельные вызовы не режут JSON друг друга пополам.
        """
        line = self._render(record)
        try:
            async with self._lock:
                await asyncio.to_thread(self._append, self.path_for(), line)
        except Exception:  # noqa: BLE001 — диск полон, прав нет и т.п.
            log.exception("не удалось записать строку лога LLM")

    def _render(self, record: dict[str, Any]) -> str:
        ordered = {"ts": record.get("ts") or utc_now_iso()}
        for key in FIELD_ORDER[1:]:
            if key in record:
                ordered[key] = record[key]
        ordered.update({k: v for k, v in record.items() if k not in ordered})
        # ensure_ascii=False: отзывы и резюме на русском, экранированный лог
        # читать невозможно. default=str — страховка от datetime/Decimal внутри.
        return json.dumps(ordered, ensure_ascii=False, default=str)

    @staticmethod
    def _append(path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


_logger: JsonlLogger | None = None


def get_jsonl_logger() -> JsonlLogger:
    """Один логгер на процесс: лока имеет смысл только общая."""
    global _logger
    if _logger is None:
        _logger = JsonlLogger()
    return _logger
