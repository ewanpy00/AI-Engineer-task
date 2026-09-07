"""Общее Jinja2-окружение и хелперы шаблонов."""

from __future__ import annotations

import re
from urllib.parse import urlencode, urlsplit

from fastapi.templating import Jinja2Templates

from app.config import get_settings

_settings = get_settings()

templates = Jinja2Templates(directory=str(_settings.templates_dir))


def cover_url(cover_path: str | None) -> str | None:
    """bucketPath -> прямой URL обложки на CDN Metacritic (research: cover_image_url)."""
    if not cover_path:
        return None
    return f"{_settings.metacritic_cdn_url}/{cover_path.lstrip('/')}"


def query_string(**params: object) -> str:
    """`?a=1&b=2` из непустых параметров; пустая строка, если непустых нет.

    Нужен и шаблонам (ссылки пагинации), и роутам (канонический URL в
    заголовке `HX-Push-Url`), чтобы обе стороны строили адрес одинаково.
    """
    pairs = [(key, str(value)) for key, value in params.items() if value not in (None, "")]
    return "?" + urlencode(pairs) if pairs else ""


def views(count: int | None) -> str:
    """`1234567` -> `1 234 567`: число просмотров летсплея (T-44).

    Без разделителя разрядов семизначное число читается как случайный набор
    цифр. Разделитель — тонкий пробел, чтобы строка не переносилась по нему.
    """
    return f"{count:,}".replace(",", "\u2009") if isinstance(count, int) else ""


# Схемы, которые можно подставить в `src`/`href` чужой ссылки. `javascript:`
# в `<iframe src>` и в `<a href>` исполняется в origin нашей страницы, поэтому
# белый список, а не чёрный.
_WEB_SCHEMES = frozenset({"http", "https"})

# Ошибки собираются как `причина: текст` — до двоеточия стоит машинная причина
# (`MetacriticError`, `no_session`, `schema`), после неё произвольный текст.
# Отдельным вариантом — HTTP-код: у отказов LLM причина именно код
# (`app.llm.gemini_client.llm_error`), и `429` на странице статуса должен
# читаться как исчерпанная квота бесплатного тира, а не как «ошибка».
_REASON_RE = re.compile(r"^(?:[1-5][0-9]{2}|[A-Za-z_][A-Za-z0-9_.]{0,63})$")
UNKNOWN_REASON = "ошибка"


def external_url(url: str | None) -> str | None:
    """Ссылка из внешнего API — или `None`, если её схеме нельзя доверять.

    `video_url` приходит из ответа Metacritic и попадает в `<iframe src>` и в
    `<a href>`. Автоэкранирование Jinja не выпускает значение за пределы
    атрибута, но схему не проверяет: `javascript:…` в обоих случаях исполнится
    как скрипт нашей страницы. Всё, кроме http(s), шаблон должен считать
    отсутствующей ссылкой.
    """
    if not url:
        return None
    try:
        scheme = urlsplit(url.strip()).scheme.lower()
    except ValueError:  # невалидный URL — та же отсутствующая ссылка
        return None
    return url.strip() if scheme in _WEB_SCHEMES else None


def error_kind(error: str | None) -> str:
    """Причина отказа без текста исключения — для публичных страниц.

    `/status` открыт всем (ТЗ требует мониторинг в вебе), а в тексте
    исключения лежат хосты, пути эндпоинтов и параметры запросов — иногда и
    строка подключения к БД. Наружу отдаём только машинную причину
    (`MetacriticError`, `no_session`), полный текст остаётся в `runs.error`,
    `processed_games.error` и в логе процесса.
    """
    if not error:
        return ""
    reason = error.split(":", 1)[0].strip()
    return reason if _REASON_RE.match(reason) else UNKNOWN_REASON


templates.env.globals["cover_url"] = cover_url
templates.env.globals["query_string"] = query_string
templates.env.globals["external_url"] = external_url
templates.env.filters["views"] = views
templates.env.filters["error_kind"] = error_kind
