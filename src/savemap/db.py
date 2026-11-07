"""SQLite storage. One `videos` row per saved video; `state` tracks pipeline progress.

State machine:  ingested -> downloaded -> transcribed -> enriched
Terminal side state: unavailable (deleted/private). Failures keep the current state and
bump `attempts`; a row is retried until attempts reaches pipeline.max_attempts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

STATES = ("ingested", "downloaded", "transcribed", "enriched", "unavailable")

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id                INTEGER PRIMARY KEY,
    platform          TEXT NOT NULL,
    video_id          TEXT NOT NULL,
    url               TEXT NOT NULL,
    saved_at          TEXT,
    source            TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'ingested',
    error_stage       TEXT,
    last_error        TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    author            TEXT,
    caption           TEXT,
    hashtags          TEXT,          -- JSON list
    duration          REAL,
    audio_path        TEXT,
    transcript        TEXT,
    transcript_source TEXT,
    needs_visual      INTEGER NOT NULL DEFAULT 0,
    enrichment        TEXT,          -- JSON object (validated Enrichment)
    slug              TEXT UNIQUE,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (platform, video_id)
);
CREATE INDEX IF NOT EXISTS videos_state ON videos (state, attempts);

CREATE TABLE IF NOT EXISTS topic_overviews (
    topic       TEXT PRIMARY KEY,
    video_count INTEGER NOT NULL,
    text        TEXT NOT NULL
);
"""

# Columns stages may write via update(); guards against typos in field names.
_WRITABLE = {
    "video_id", "url", "state", "error_stage", "last_error", "attempts",
    "author", "caption", "hashtags", "duration", "audio_path",
    "transcript", "transcript_source", "needs_visual", "enrichment", "slug",
}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def insert_video(
    conn: sqlite3.Connection, *, platform: str, video_id: str, url: str,
    saved_at: str | None, source: str,
) -> bool:
    """Insert a new video; returns False if (platform, video_id) already exists."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO videos (platform, video_id, url, saved_at, source) VALUES (?, ?, ?, ?, ?)",
        (platform, video_id, url, saved_at, source),
    )
    if cur.rowcount == 1:
        return True
    if saved_at:  # e.g. first added via urls.txt, now seen in an export with a save date
        conn.execute("UPDATE videos SET saved_at = ? WHERE platform = ? AND video_id = ? AND saved_at IS NULL",
                     (saved_at, platform, video_id))
    return False


def pending(conn: sqlite3.Connection, state: str, max_attempts: int, limit: int | None = None) -> list[sqlite3.Row]:
    """Rows waiting for the stage that consumes `state`, oldest-saved first."""
    sql = "SELECT * FROM videos WHERE state = ? AND attempts < ? ORDER BY saved_at IS NULL, saved_at, id"
    params: list = [state, max_attempts]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def update(conn: sqlite3.Connection, row_id: int, **fields) -> None:
    bad = set(fields) - _WRITABLE
    if bad:
        raise ValueError(f"Not writable: {sorted(bad)}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE videos SET {sets}, updated_at = datetime('now') WHERE id = ?",
        (*fields.values(), row_id),
    )
    conn.commit()


def advance(conn: sqlite3.Connection, row_id: int, new_state: str, **fields) -> None:
    """Stage succeeded: move to new_state and clear error bookkeeping."""
    update(conn, row_id, state=new_state, attempts=0, error_stage=None, last_error=None, **fields)


def fail(conn: sqlite3.Connection, row_id: int, stage: str, error: str) -> None:
    conn.execute(
        "UPDATE videos SET attempts = attempts + 1, error_stage = ?, last_error = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (stage, error[:2000], row_id),
    )
    conn.commit()


def counts_by_state(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT platform, state, COUNT(*) AS n FROM videos GROUP BY platform, state ORDER BY platform"
    ).fetchall()


def recent_errors(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, platform, url, state, error_stage, attempts, last_error FROM videos "
        "WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
