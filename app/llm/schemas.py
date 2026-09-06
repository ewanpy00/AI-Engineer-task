"""Контракты LLM-слоя из design §4.4: схемы ответа и результат вызова.

Схемы — Pydantic-модели, и они же уезжают в Gemini как `responseSchema`
(11-decisions.md, поправка к ADR-7: структурированный вывод остаётся,
механизм меняется с tool use на `responseSchema`). Ограничения полей
попадают в JSON-схему запроса, поэтому описания здесь — часть промпта,
а не документация для читателя кода.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, field_validator

T = TypeVar("T")

LIKED_MAX = 5
DISLIKED_MAX = 5
TLDR_MAX = 300


def _clip_items(value: Any, limit: int) -> Any:
    """Обрезает список до `limit` непустых пунктов, не роняя валидацию.

    Модель периодически отдаёт на пункт-два больше запрошенного. Лишний пункт —
    не повод потерять весь вызов вместе с потраченными токенами, поэтому режем
    здесь, до проверки ограничений.
    """
    if not isinstance(value, list):
        return value
    items = [str(v).strip() for v in value if str(v).strip()]
    return items[:limit]


class ReviewSummaryOut(BaseModel):
    """Резюме отзывов одной аудитории по одной игре."""

    liked: list[str] = Field(
        min_length=2,
        max_length=LIKED_MAX,
        description="Что нравится: 2-5 коротких пунктов на русском языке",
    )
    disliked: list[str] = Field(
        default_factory=list,
        max_length=DISLIKED_MAX,
        description="Что не нравится: 0-5 коротких пунктов на русском языке",
    )
    tldr: str = Field(
        max_length=TLDR_MAX,
        description="Итог одним-двумя предложениями на русском, не длиннее 300 символов",
    )

    @field_validator("liked", "disliked", mode="before")
    @classmethod
    def _clip_lists(cls, value: Any) -> Any:
        return _clip_items(value, LIKED_MAX)

    @field_validator("tldr", mode="before")
    @classmethod
    def _clip_tldr(cls, value: Any) -> Any:
        return str(value).strip()[:TLDR_MAX] if isinstance(value, str) else value


class LetsplayConclusionOut(BaseModel):
    """Заключение по пересказу летсплея (доп. часть 1, T-43)."""

    conclusion: str = Field(
        max_length=1200,
        description="Заключение о игре по пересказу прохождения, на русском языке",
    )

    @field_validator("conclusion", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


@dataclass(frozen=True)
class LlmResult(Generic[T]):
    """Исход одного обращения к модели. Исключений наружу адаптер не выпускает.

    `ok=False` — это штатное значение, а не авария: резюме отзывов обогащает
    карточку, и его отсутствие не должно ронять обход каталога (design §5.5).
    """

    ok: bool
    value: T | None = None
    error: str | None = None
    prompt_version: str = ""
    prompt_sha256: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 0
