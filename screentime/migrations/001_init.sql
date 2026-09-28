-- ScreenTime initial schema
-- Design notes:
--   * apps: one row per distinct application identity (grouped by wm_class/app_id),
--     never deleted, so history survives even if the app is later excluded/uninstalled.
--   * sessions: append-only log of focused-usage intervals. This is the source of
--     truth. Everything else (daily_totals) is a derived/materialized cache that
--     can always be rebuilt from `sessions` alone.
--   * daily_totals: materialized per-app-per-day seconds, kept in sync incrementally
--     for fast dashboard queries without scanning the whole sessions table.
--   * A session never spans two local calendar days on disk -- the daemon splits
--     a session at local midnight into two rows. This keeps day/week/month
--     aggregation a trivial GROUP BY instead of interval-splitting at query time.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS apps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT UNIQUE NOT NULL,      -- stable canonical id (lowercased wm_class/app_id/exe)
    display_name  TEXT NOT NULL,
    icon_name     TEXT,                      -- freedesktop icon name, if resolved
    desktop_file  TEXT,                      -- path to matched .desktop file, if any
    excluded      INTEGER NOT NULL DEFAULT 0,
    first_seen    INTEGER NOT NULL,          -- unix epoch seconds
    last_seen     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id         INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    start_time     INTEGER NOT NULL,   -- unix epoch seconds (wall clock, corrected for clock jumps)
    end_time       INTEGER,            -- NULL while the session is still open
    last_heartbeat INTEGER,            -- progress checkpoint written every daemon tick, for crash recovery;
                                        -- independent of end_time so "is this session open" stays a simple
                                        -- `end_time IS NULL` check throughout its whole open lifetime.
    day            TEXT NOT NULL,      -- local 'YYYY-MM-DD' the session belongs to (always single-day)
    end_reason     TEXT,               -- focus_change | idle | suspend | shutdown | daemon_stop | crash_recovered | midnight_split
    CHECK (end_time IS NULL OR end_time >= start_time)
);

CREATE INDEX IF NOT EXISTS idx_sessions_app       ON sessions(app_id);
CREATE INDEX IF NOT EXISTS idx_sessions_day        ON sessions(day);
CREATE INDEX IF NOT EXISTS idx_sessions_open       ON sessions(end_time) WHERE end_time IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_app_day     ON sessions(app_id, day);

CREATE TABLE IF NOT EXISTS daily_totals (
    app_id        INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    day           TEXT NOT NULL,
    seconds       INTEGER NOT NULL DEFAULT 0,
    session_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, day)
);

CREATE INDEX IF NOT EXISTS idx_daily_totals_day ON daily_totals(day);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

INSERT INTO schema_meta(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_meta);
