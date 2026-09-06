-- Схема БД. DDL из docs/10-design.md §3, применяется идемпотентно при старте
-- приложения (ADR-3: без Alembic и без Base.metadata.create_all).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- §3.1 Справочные и контентные таблицы -------------------------------------

CREATE TABLE IF NOT EXISTS games (
    id                 bigint      PRIMARY KEY,          -- id из Metacritic (стабильный)
    slug               text        NOT NULL UNIQUE,
    title              text        NOT NULL,
    description        text,
    developer          text,
    publisher          text,
    esrb_rating        text,
    release_date       date,
    cover_path         text,        -- bucketPath, URL строится в шаблоне
    video_url          text,        -- официальный трейлер Metacritic (embedUrl)
    lead_platform_slug text,
    -- денормализация ради сортировки списка одним индексным сканом:
    best_metascore     smallint,    -- max(metascore) по платформам, NULL если нет оценок
    best_userscore     numeric(3,1),
    genres_cache       text[]       NOT NULL DEFAULT '{}',  -- для показа в списке без join
    raw                jsonb        NOT NULL,               -- сырой ответ product, для дебага
    first_seen_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at         timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS games_title_trgm_idx
    ON games USING gin (title gin_trgm_ops);              -- поиск по названию ILIKE %q%
CREATE INDEX IF NOT EXISTS games_best_metascore_idx
    ON games (best_metascore DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS games_best_userscore_idx
    ON games (best_userscore DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS games_first_seen_idx ON games (first_seen_at DESC);

CREATE TABLE IF NOT EXISTS game_platforms (
    game_id             bigint      NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    platform_slug       text        NOT NULL,             -- pc, playstation-5, ...
    platform_name       text        NOT NULL,
    is_lead             boolean     NOT NULL DEFAULT false,
    release_date        date,
    metascore           smallint,                          -- 0..100, из product
    metascore_count     integer,
    metascore_sentiment text,
    userscore           numeric(3,1),                      -- 0..10, отдельный вызов
    userscore_count     integer,
    userscore_sentiment text,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, platform_slug)
);
CREATE INDEX IF NOT EXISTS game_platforms_slug_idx ON game_platforms (platform_slug);

CREATE TABLE IF NOT EXISTS game_genres (
    game_id bigint NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    genre   text   NOT NULL,
    PRIMARY KEY (game_id, genre)
);
CREATE INDEX IF NOT EXISTS game_genres_genre_idx ON game_genres (genre);

-- §3.2 Резюме отзывов и летсплей --------------------------------------------

CREATE TABLE IF NOT EXISTS review_summaries (
    game_id        bigint NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    audience       text   NOT NULL CHECK (audience IN ('critic','user')),
    platform_slug  text   NOT NULL,               -- ведущая платформа на момент генерации
    liked          jsonb  NOT NULL DEFAULT '[]',  -- string[]
    disliked       jsonb  NOT NULL DEFAULT '[]',  -- string[]
    tldr           text,
    quotes_count   integer NOT NULL DEFAULT 0,
    quotes_hash    text,                          -- sha256 нормализованного набора цитат
    source         text CHECK (source IN ('summary_endpoint','review_list')),
    status         text NOT NULL DEFAULT 'ok'
                   CHECK (status IN ('ok','no_data','llm_failed')),
    prompt_version text,
    model          text,
    error          text,
    generated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, audience)
);

CREATE TABLE IF NOT EXISTS letsplays (
    game_id       bigint PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    status        text NOT NULL
                  CHECK (status IN ('ok','not_found','service_error','disabled')),
    video_id      text,
    video_url     text,
    video_title   text,
    channel       text,
    view_count    bigint,
    retelling     text,        -- пересказ от 300.ya.ru
    conclusion    text,        -- короткое заключение, LLM поверх пересказа
    attempts      smallint NOT NULL DEFAULT 0,
    error         text,
    last_attempt_at timestamptz,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- §3.3 Состояние обхода: курсор дня и журнал обработанных --------------------

CREATE TABLE IF NOT EXISTS day_cursor (
    day            date    PRIMARY KEY,              -- UTC-дата
    phase          text    NOT NULL DEFAULT 'new_releases'
                   CHECK (phase IN ('new_releases','browse','exhausted')),
    browse_offset  integer NOT NULL DEFAULT 0,       -- следующий offset для SEE ALL
    runs_count     integer NOT NULL DEFAULT 0,
    claimed_count  integer NOT NULL DEFAULT 0,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS processed_games (
    day           date   NOT NULL,
    game_id       bigint NOT NULL,                    -- FK намеренно нет: клеймим до вставки в games
    slug          text   NOT NULL,
    source        text   NOT NULL CHECK (source IN ('new_releases','browse','manual')),
    status        text   NOT NULL DEFAULT 'claimed'
                  CHECK (status IN ('claimed','ok','failed')),
    run_id        bigint,
    error         text,
    claimed_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    PRIMARY KEY (day, game_id)
);
CREATE INDEX IF NOT EXISTS processed_games_day_status_idx ON processed_games (day, status);
CREATE INDEX IF NOT EXISTS processed_games_run_idx ON processed_games (run_id);

-- §3.4 Журнал заходов --------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    id           bigserial PRIMARY KEY,
    day          date NOT NULL,
    trigger      text NOT NULL CHECK (trigger IN ('schedule','manual')),
    status       text NOT NULL
                 CHECK (status IN ('running','ok','empty','failed','skipped_locked')),
    phase        text,
    pages_fetched     integer NOT NULL DEFAULT 0,
    games_claimed     integer NOT NULL DEFAULT 0,
    games_ok          integer NOT NULL DEFAULT 0,
    games_failed      integer NOT NULL DEFAULT 0,
    llm_calls         integer NOT NULL DEFAULT 0,
    llm_failures      integer NOT NULL DEFAULT 0,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    error        text
);
CREATE INDEX IF NOT EXISTS runs_started_idx ON runs (started_at DESC);
