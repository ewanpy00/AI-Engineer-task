"""T-41: поиск самого популярного летсплея через yt-dlp `ytsearch`.

Почему не официальный YouTube Data API: `search.list` стоит 100 unit при
дневной квоте 10 000, то есть не больше 100 поисков в сутки на ключ, а игр за
сутки может быть до 480 (research §5.1). `ytsearch` квот не имеет и на обычной
машине проверен вживую: по запросу вида `"<игра> let's play"` возвращаются
`id/title/view_count/duration` (research §5.2).

Тем же research зафиксирован и риск: с датацентрового IP YouTube отвечает
`LOGINREQUIRED` ещё до выдачи ссылок на форматы (ADR-8). Поэтому поиск здесь —
best-effort: любая ошибка гасится и наружу уходит `None`, а не исключение.
Пустой результат для всех игр — предусмотренный сценарий, он виден в БД как
`letsplays.status='not_found'`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Sequence

from app.config import Settings, get_settings
from app.letsplay.dto import YOUTUBE_WATCH_URL, VideoCandidate

log = logging.getLogger(__name__)

# Формулировка запроса — та же, что проверялась в research §5.2
# (`"Onimusha Way of the Sword let's play"`), с ней выдача состоит из
# прохождений, а не из трейлеров и обзоров.
SEARCH_QUERY = "{title} let's play"

# Плоское извлечение: нам нужны только метаданные выдачи, а не форматы каждого
# ролика. Оно же обходит стороной ту часть, где YouTube требует PO-token —
# запрос к самому видео не делается вовсе.
YDL_OPTS: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": True,
    "noplaylist": True,
    "ignoreerrors": True,
    "retries": 1,
}

SearchFn = Callable[[str, int, float], Sequence[dict[str, Any]]]


def ytdlp_search(query: str, count: int, timeout_s: float) -> Sequence[dict[str, Any]]:
    """Синхронный вызов yt-dlp. Единственная точка выхода в сеть этого модуля.

    Импорт локальный: yt-dlp тянет за собой заметное дерево модулей, а заход
    без летсплеев (LETSPLAY_ENABLED=false) до него не доходит.
    """
    from yt_dlp import YoutubeDL

    opts = YDL_OPTS | {"socket_timeout": timeout_s}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
    entries = (info or {}).get("entries") or []
    return [entry for entry in entries if isinstance(entry, dict)]


def to_candidate(entry: dict[str, Any]) -> VideoCandidate | None:
    """Запись выдачи → DTO. Без `id` кандидат бесполезен: ссылку не построить."""
    video_id = entry.get("id")
    if not video_id:
        return None
    return VideoCandidate(
        video_id=str(video_id),
        # Ссылку собираем сами, а не берём `entry["url"]`: в плоской выдаче там
        # встречается и короткая, и внутренняя форма, а в карточку и в БД
        # должен уйти один канонический вид.
        video_url=YOUTUBE_WATCH_URL.format(video_id=video_id),
        title=str(entry.get("title") or "").strip() or str(video_id),
        channel=(entry.get("channel") or entry.get("uploader") or None),
        view_count=_as_int(entry.get("view_count")),
        duration_s=_as_int(entry.get("duration")),
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def pick_most_viewed(
    candidates: Sequence[VideoCandidate], min_duration_s: int
) -> VideoCandidate | None:
    """Самый просматриваемый ролик — с поправкой на длительность.

    ТЗ просит «самый популярный летсплей», но по просмотрам в выдаче обычно
    выигрывает трейлер или шортс, где рассказа блогера нет вовсе. Поэтому
    сначала смотрим только на достаточно длинные ролики и лишь если таких нет
    (длительность в выдаче не пришла, все ролики короткие) — на всю выдачу:
    пустой результат здесь означал бы `not_found` там, где ролик на самом деле
    есть.
    """
    if not candidates:
        return None
    long_enough = [
        c for c in candidates if c.duration_s is not None and c.duration_s >= min_duration_s
    ]
    pool = long_enough or list(candidates)
    return max(pool, key=lambda c: (c.view_count or 0, c.duration_s or 0))


class YtDlpFinder:
    """Реализация `LetsplayFinder` из design §4.5."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        search: SearchFn | None = None,
        slots: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._search = search or ytdlp_search
        # Общий с пересказом потолок задаёт пайплайн; свой — чтобы finder был
        # пригоден и отдельно (тесты, ручная проверка ADR-8 на Railway).
        self._slots = slots or asyncio.Semaphore(
            max(1, self._settings.letsplay_max_concurrency)
        )

    async def find(self, game_title: str) -> VideoCandidate | None:
        """Кандидат или `None`. Наружу не бросает ничего (DoD T-41)."""
        query = SEARCH_QUERY.format(title=game_title.strip())
        try:
            entries = await self._entries(query)
        except Exception as exc:  # noqa: BLE001 — фича best-effort, ADR-8
            log.warning("поиск летсплея «%s» не удался: %s: %s", query, type(exc).__name__, exc)
            return None

        candidates = [c for c in map(to_candidate, entries) if c is not None]
        if not candidates:
            log.info("по запросу «%s» yt-dlp ничего не вернул", query)
            return None

        picked = pick_most_viewed(candidates, self._settings.letsplay_min_duration_s)
        if picked is not None:
            log.info(
                "летсплей для «%s»: %s (%s просмотров)",
                game_title, picked.video_url, picked.view_count,
            )
        return picked

    async def _entries(self, query: str) -> Sequence[dict[str, Any]]:
        """yt-dlp синхронный — уводим его в поток, чтобы не встать event loop'ом.

        `to_thread`, а не свой пул: потоков ровно столько, сколько пропустит
        семафор, и они короткоживущие. Таймаут задаётся самому yt-dlp
        (`socket_timeout`): отмена `to_thread` снаружи всё равно не прервала бы
        уже начатый сетевой вызов, а бросить его недоделанным хуже, чем дождаться.
        """
        async with self._slots:
            return await asyncio.to_thread(
                self._search,
                query,
                max(1, self._settings.letsplay_search_results),
                self._settings.letsplay_search_timeout_s,
            )
