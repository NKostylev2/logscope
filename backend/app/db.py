import sqlite3
from contextlib import contextmanager
from .config import DATA_DIR, DB_PATH

TABLES_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_count INTEGER NOT NULL DEFAULT 0,
    min_time_ms INTEGER,
    max_time_ms INTEGER,
    parse_errors INTEGER NOT NULL DEFAULT 0,
    unparsed_timestamps INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    timestamp_utc_ms INTEGER,
    timestamp_ms INTEGER,
    timestamp_raw TEXT,
    level TEXT,
    message TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    request_id TEXT,
    duration_ms INTEGER,
    event_type TEXT NOT NULL DEFAULT 'log',
    fields_json TEXT NOT NULL DEFAULT '{}'
);
"""

INDEXES_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_events_time ON events(timestamp_utc_ms, id);
CREATE INDEX IF NOT EXISTS idx_events_source_time ON events(source_id, timestamp_utc_ms, id);
CREATE INDEX IF NOT EXISTS idx_events_source_utc_time ON events(source_id, timestamp_utc_ms, id);
CREATE INDEX IF NOT EXISTS idx_events_request_id ON events(request_id);
CREATE INDEX IF NOT EXISTS idx_events_level_time ON events(level, timestamp_utc_ms);
CREATE INDEX IF NOT EXISTS idx_events_legacy_time ON events(timestamp_ms, id);
CREATE INDEX IF NOT EXISTS idx_events_resp_code ON events(json_extract(fields_json, '$.responseCode'));
CREATE INDEX IF NOT EXISTS idx_events_status ON events(json_extract(fields_json, '$.status'));
"""


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with connect() as conn:
        # 1. Create base tables
        conn.executescript(TABLES_SCHEMA)

        # 2. Check and migrate columns on existing databases
        src_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "sha256" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN sha256 TEXT")
        if "unparsed_timestamps" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN unparsed_timestamps INTEGER NOT NULL DEFAULT 0")
        if "min_time_ms" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN min_time_ms INTEGER")
        if "max_time_ms" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN max_time_ms INTEGER")
        if "status" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'")
        if "error_message" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN error_message TEXT")

        # Backfill min_time_ms / max_time_ms if null
        conn.execute("""
            UPDATE sources
            SET min_time_ms = (SELECT MIN(timestamp_utc_ms) FROM events WHERE source_id = sources.id),
                max_time_ms = (SELECT MAX(timestamp_utc_ms) FROM events WHERE source_id = sources.id)
            WHERE min_time_ms IS NULL AND event_count > 0
        """)

        evt_cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "timestamp_utc_ms" not in evt_cols:
            conn.execute("ALTER TABLE events ADD COLUMN timestamp_utc_ms INTEGER")
            if "timestamp_ms" in evt_cols:
                conn.execute("UPDATE events SET timestamp_utc_ms = timestamp_ms WHERE timestamp_utc_ms IS NULL")
        if "timestamp_ms" not in evt_cols:
            conn.execute("ALTER TABLE events ADD COLUMN timestamp_ms INTEGER")
            conn.execute("UPDATE events SET timestamp_ms = timestamp_utc_ms WHERE timestamp_ms IS NULL")
        if "duration_ms" not in evt_cols:
            conn.execute("ALTER TABLE events ADD COLUMN duration_ms INTEGER")
        if "event_type" not in evt_cols:
            conn.execute("ALTER TABLE events ADD COLUMN event_type TEXT NOT NULL DEFAULT 'log'")

        # 3. Create indexes now that all columns are guaranteed to exist
        conn.executescript(INDEXES_SCHEMA)
        conn.commit()


@contextmanager
def transaction():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
