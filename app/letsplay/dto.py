"""Контракты слоя летсплеев (design §4.5).

Своё DTO, а не общий `app/clients/dto.py`: там живут контракты Metacritic, а
здесь — YouTube и 300.ya.ru. Единственное, что связывает эти два мира, —
`game_id`, и связывает их пайплайн, а не структуры данных.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Статусы из DDL `letsplays.status` (design §3.2). `disabled` — не ошибка:
# так помечается игра, для которой фича выключена (нет куки 300.ya.ru или
# LETSPLAY_ENABLED=false), чтобы это отличалось от неудачи сервиса.
LetsplayStatus = Literal["ok", "not_found", "service_error", "disabled"]

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


@dataclass(frozen=True)
class VideoCandidate:
    """Найденный ролик. Поля — ровно те, что отдаёт `ytsearch` (research §5.2)."""

    video_id: str
    video_url: str
    title: str
    channel: str | None = None
    view_count: int | None = None
    duration_s: int | None = None


@dataclass(frozen=True)
class LetsplayResult:
    """Исход одной попытки обогащения. Наружу пайплайн бросает только это.

    `llm_calls`/`llm_failures` считаются здесь по той же причине, что в
    `review_pipeline.Outcome`: заключение по пересказу — третье обращение к
    модели (design §5.1), и в `runs` оно должно попасть вместе с остальными.
    """

    status: LetsplayStatus
    video: VideoCandidate | None = None
    conclusion: str | None = None
    error: str | None = None
    llm_calls: int = 0
    llm_failures: int = 0
