"""T-25: загрузка промптов из `prompts/*.md`. В коде — только загрузка.

Правило проекта (CLAUDE.md, design §5.2): текст промпта и параметры генерации
живут в файле рядом друг с другом, версия — суффикс в имени
(`{name}.v{N}.md`), новая версия = новый файл, старый не редактируется.
Отсюда `sha256` — он ловит ровно тот случай, который версия не ловит: файл
поправили, не подняв номер.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from app.config import Settings, get_settings

_VERSION_RE = re.compile(r"^(?P<name>.+)\.v(?P<num>\d+)$")
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(?P<meta>.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


class PromptError(LookupError):
    """Промпт не найден или не читается — с указанием, где именно искали.

    Отдельный тип нужен, чтобы наружу не улетал голый `FileNotFoundError`:
    по нему не видно ни имени промпта, ни каталога, в котором его ждали.
    """


def _parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    """Разбирает `---`-шапку в плоский словарь строк и возвращает тело.

    Намеренно не YAML: в шапке промпта живут только скаляры
    (`model`, `max_tokens`, `temperature`, описание схемы), а тащить ради
    четырёх строк парсер YAML в зависимости незачем.
    """
    match = _FRONT_MATTER_RE.match(raw)
    if match is None:
        return {}, raw.strip()

    meta: dict[str, str] = {}
    for line in match.group("meta").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip("\"'")
    return meta, raw[match.end() :].strip()


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Prompt:
    """Загруженный промпт: контракт design §4.4 плюс параметры генерации.

    `text` — только тело system-промпта. `meta` — шапка файла: design §5.2
    требует, чтобы `model`/`max_tokens`/`temperature` лежали рядом с текстом,
    а не были зашиты в код адаптера.
    """

    name: str
    version: str
    text: str
    sha256: str
    path: Path | None = None
    meta: Mapping[str, str] = field(default_factory=dict)

    @property
    def version_tag(self) -> str:
        """То, что уходит в `prompt_version` логов и в `review_summaries`."""
        return f"{self.name}.{self.version}"

    @property
    def model(self) -> str | None:
        return self.meta.get("model") or None

    @property
    def max_tokens(self) -> int | None:
        return _as_int(self.meta.get("max_tokens"))

    @property
    def temperature(self) -> float | None:
        return _as_float(self.meta.get("temperature"))

    @property
    def thinking_level(self) -> str | None:
        """Глубина «размышлений» модели — параметр Gemini 3.x.

        Живёт в файле рядом с текстом по той же причине, что `temperature`:
        он меняет ответ. Для резюмирования разница измерена на живом API —
        см. комментарий в `gemini_client._config`.
        """
        value = self.meta.get("thinking_level")
        return value.upper() if value else None


class PromptRepo:
    """Реализация `PromptRepo` из design §4.4. Кэш — на процесс.

    Кэш без инвалидации намеренно: промпт неизменен в пределах запуска, и
    именно это делает `sha256` в логе честным — все строки одного процесса
    ссылаются на один и тот же текст.
    """

    def __init__(self, directory: Path | None = None, settings: Settings | None = None) -> None:
        self._dir = directory or (settings or get_settings()).prompts_dir
        self._cache: dict[str, Prompt] = {}

    @property
    def directory(self) -> Path:
        return self._dir

    def load(self, name: str) -> Prompt:
        """Грузит `prompts/{name}.v{N}.md`, при нескольких версиях — старшую.

        `name` можно передать и с версией (`review_summary_user.v1`) — так
        закрепляется конкретная версия, если старшая ещё обкатывается.
        """
        cached = self._cache.get(name)
        if cached is not None:
            return cached

        path = self._resolve(name)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PromptError(f"промпт {name}: файл {path} не читается ({exc})") from exc

        base, version = _split_version(path.stem)
        meta, text = _parse_front_matter(raw)
        if not text:
            raise PromptError(f"промпт {name}: файл {path} пуст после front-matter")

        prompt = Prompt(
            name=base,
            version=version,
            text=text,
            # хешируем файл целиком, а не только тело: правку `temperature`
            # в шапке версия тоже не ловит, а на ответ модели она влияет
            sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            path=path,
            meta=meta,
        )
        self._cache[name] = prompt
        return prompt

    def _resolve(self, name: str) -> Path:
        if _VERSION_RE.match(name):  # имя уже с версией — берём ровно его
            path = self._dir / f"{name}.md"
            if not path.is_file():
                raise PromptError(f"промпт {name}: нет файла {path}")
            return path

        candidates = sorted(
            (p for p in self._dir.glob(f"{name}.v*.md") if _VERSION_RE.match(p.stem)),
            key=lambda p: int(_VERSION_RE.match(p.stem)["num"]),  # type: ignore[index]
        )
        if not candidates:
            raise PromptError(
                f"промпт {name}: в {self._dir} нет ни одного файла {name}.v*.md "
                f"(есть: {', '.join(sorted(p.name for p in self._dir.glob('*.md'))) or 'ничего'})"
            )
        return candidates[-1]


def _split_version(stem: str) -> tuple[str, str]:
    match = _VERSION_RE.match(stem)
    if match is None:  # сюда не попасть: имя проверено в `_resolve`
        return stem, "v0"
    return match["name"], f"v{int(match['num'])}"


_repo: PromptRepo | None = None


def get_prompt_repo() -> PromptRepo:
    """Один репозиторий на процесс — иначе кэш теряет смысл."""
    global _repo
    if _repo is None:
        _repo = PromptRepo()
    return _repo
