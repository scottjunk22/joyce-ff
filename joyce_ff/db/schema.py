"""
SQLite schema and helpers.

Core principle: APPEND-ONLY. Every scrape of the league site writes a new
`scrape_snapshots` row (with the raw text and a content hash), and all parsed
rows reference that snapshot_id. We never UPDATE or DELETE historical rows —
the diffs between snapshots are how we detect roster moves, trades, and
lineup changes.

NFL stats from nflverse are cached in their own tables keyed by (season, week)
plus the fetch time, so we can tell how stale they are without overwriting.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "joyce_ff.sqlite"


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Every fetch of the league site is recorded here, raw and immutable.
CREATE TABLE IF NOT EXISTS scrape_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,            -- e.g. 'league_site'
    url            TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,            -- ISO-8601
    content_sha256 TEXT NOT NULL,
    raw_text       TEXT NOT NULL,
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap_source_time
    ON scrape_snapshots(source, fetched_at_utc);

-- Fantasy teams (the 22 league entrants), tagged by conference.
CREATE TABLE IF NOT EXISTS fantasy_teams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    conference  TEXT CHECK (conference IN ('BLUE', 'RED'))
);

-- Weekly lineup rows parsed from the site. asset_kind distinguishes a team
-- unit ('TEAM_UNIT') from an individual player ('PLAYER').
CREATE TABLE IF NOT EXISTS lineups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id   INTEGER NOT NULL REFERENCES scrape_snapshots(id),
    season        INTEGER NOT NULL,
    week          INTEGER NOT NULL,
    fantasy_team  TEXT NOT NULL,
    slot          TEXT NOT NULL,             -- C, K, DEF/ST, QB, RB, R
    slot_index    INTEGER NOT NULL DEFAULT 0,-- 0..2 for RB, 0..3 for R
    asset_kind    TEXT NOT NULL CHECK (asset_kind IN ('TEAM_UNIT', 'PLAYER')),
    asset_name    TEXT NOT NULL,             -- NFL team (unit) or player name
    started       INTEGER NOT NULL DEFAULT 1 -- 1 = in starting 9, 0 = bench flex
);
CREATE INDEX IF NOT EXISTS ix_lineups_lookup
    ON lineups(season, week, fantasy_team);

-- Weekly scores AS POSTED ON THE SITE. This is the reconciliation target:
-- our engine must reproduce these from nflverse box scores.
CREATE TABLE IF NOT EXISTS posted_weekly_scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id   INTEGER NOT NULL REFERENCES scrape_snapshots(id),
    season        INTEGER NOT NULL,
    week          INTEGER NOT NULL,
    fantasy_team  TEXT NOT NULL,
    posted_points REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_posted_scores_lookup
    ON posted_weekly_scores(season, week, fantasy_team);

-- nflverse per-game player stats cache (raw counts only).
CREATE TABLE IF NOT EXISTS nfl_player_games (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at_utc  TEXT NOT NULL,
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    player_id       TEXT,
    player          TEXT NOT NULL,
    team            TEXT NOT NULL,
    rushing_yards   REAL DEFAULT 0,
    rushing_tds     REAL DEFAULT 0,
    receiving_yards REAL DEFAULT 0,
    receptions      REAL DEFAULT 0,
    receiving_tds   REAL DEFAULT 0,
    passing_yards   REAL DEFAULT 0,
    passing_tds     REAL DEFAULT 0,
    return_tds      REAL DEFAULT 0,
    two_point_convs REAL DEFAULT 0,
    UNIQUE(season, week, player_id, team)
);
CREATE INDEX IF NOT EXISTS ix_nfl_player_lookup
    ON nfl_player_games(season, week, team);

-- nflverse per-game team unit stats cache (QB unit, K, DEF/ST, coach result).
CREATE TABLE IF NOT EXISTS nfl_team_games (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at_utc     TEXT NOT NULL,
    season             INTEGER NOT NULL,
    week               INTEGER NOT NULL,
    team               TEXT NOT NULL,
    won                INTEGER,
    tied               INTEGER,
    team_passing_yards REAL DEFAULT 0,
    team_passing_tds   REAL DEFAULT 0,
    fg_distances_json  TEXT DEFAULT '[]',   -- JSON list of made-FG distances
    extra_points_made  REAL DEFAULT 0,
    points_allowed     REAL,
    yards_allowed      REAL,
    sacks              REAL DEFAULT 0,
    interceptions      REAL DEFAULT 0,
    fumble_recoveries  REAL DEFAULT 0,
    safeties           REAL DEFAULT 0,
    defensive_tds      REAL DEFAULT 0,
    special_teams_tds  REAL DEFAULT 0,
    UNIQUE(season, week, team)
);
CREATE INDEX IF NOT EXISTS ix_nfl_team_lookup
    ON nfl_team_games(season, week, team);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating parent dir if needed) a SQLite connection."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if they do not exist. Idempotent."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def record_snapshot(
    conn: sqlite3.Connection,
    *,
    source: str,
    url: str,
    raw_text: str,
    notes: str | None = None,
) -> int:
    """Append an immutable snapshot of raw fetched content. Returns its id."""
    fetched_at = datetime.now(timezone.utc).isoformat()
    sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    cur = conn.execute(
        """INSERT INTO scrape_snapshots
               (source, url, fetched_at_utc, content_sha256, raw_text, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (source, url, fetched_at, sha, raw_text, notes),
    )
    conn.commit()
    return int(cur.lastrowid)
