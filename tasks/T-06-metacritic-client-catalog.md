# T-06 — Metacritic client: каталог (new releases, browse)

Срез: S1. Обязательная часть: да.

## Цель

Реализовать получение списков игр-кандидатов: New Releases для первого захода
дня и SEE ALL (browse) для последующих — контракт `MetacriticClient` из
design §4.1.

## Файлы

- `app/clients/metacritic.py` — `list_new_releases(limit=20) ->
  list[CatalogItem]`, `list_browse(offset, limit=20) -> BrowsePage`
- `app/clients/dto.py` (или в том же файле) — `CatalogItem`, `BrowsePage`
  dataclasses по design §4.1

## Зависимости

T-05.

## Определение готовности

- `list_new_releases()` возвращает ≤ 20 `CatalogItem` с заполненными
  `id`, `slug`, `title`.
- `list_browse(offset=0)` и `list_browse(offset=20)` возвращают разные
  (в общем случае) наборы и корректный `total`.
- Проверено вручную одним живым запросом к каждому методу (не мок).

## Оценка

40 минут.
