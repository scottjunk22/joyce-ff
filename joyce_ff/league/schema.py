"""
League platform SQLite schema.

Design notes
------------
* One database, season-aware (every row hangs off a season). A new season is a
  new set of teams/rosters; history is preserved.
* An "asset" is either an individual PLAYER (RB/WR/TE) or a TEAM_UNIT (QB / K /
  DEF/ST / Coach). Rather than a polymorphic table we carry
  (asset_kind, asset_ref, unit_type) on the rows that reference an asset:
    - asset_kind : 'PLAYER' | 'TEAM_UNIT'
    - asset_ref  : the player's gsis_id   OR the NFL team abbr for a unit
    - unit_type  : 'QB'|'K'|'DEF/ST'|'C'  for units, NULL for players
* Rosters are APPEND-ONLY. A roster_entry is owned for [acquired_ff_week,
  released_ff_week); an active entry has released_ff_week IS NULL. This lets us
  reconstruct any week's roster and see every move.
* Availability is DERIVED (no table): an asset is available to a team in
  conference C at week W iff no active roster_entry in conference C owns it —
  because divisions draft SEPARATE pools (the same NFL asset can be owned once
  per conference).
* Money is integer CENTS. Fee per transaction = 200. A team's balance =
  sum(active transaction fees) - sum(payments).

Never store a passcode in the clear — the *_passcode_hash columns hold hashes
produced by the auth layer.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# JOYCE_DB_PATH lets the host point at a persistent DB outside the repo. The web
# app honours it, so the CLI must too — otherwise a scheduled `run-current` on
# the host would silently score a DIFFERENT database than the one the site
# serves, and nobody would see the scores appear.
DEFAULT_DB_PATH = Path(os.environ.get("JOYCE_DB_PATH")
                       or Path(__file__).resolve().parents[2] / "data" / "league.sqlite")

TRANSACTION_FEE_CENTS = 200
ROSTER_SLOTS = ("C", "K", "DEF/ST", "QB", "RB", "R")
UNIT_TYPES = ("QB", "K", "DEF/ST", "C")

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---- reference ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS seasons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    year            INTEGER NOT NULL UNIQUE,     -- 2026 = the 2026-27 season
    label           TEXT NOT NULL,               -- '2026-27'
    current_ff_week INTEGER NOT NULL DEFAULT 0,  -- 0 = pre-draft
    ff_start_nfl_week INTEGER NOT NULL DEFAULT 3,-- FF wk1 = NFL wk3 (assumption)
    status          TEXT NOT NULL DEFAULT 'setup'-- setup|drafting|active|complete
);

CREATE TABLE IF NOT EXISTS conferences (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE CHECK (code IN ('BLUE','RED')),
    name TEXT NOT NULL
);

-- Commissioners (Steve & Scott). Shared admin powers, each own passcode.
CREATE TABLE IF NOT EXISTS admins (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    passcode_hash  TEXT,
    created_at     TEXT NOT NULL
);

-- ---- fantasy teams ------------------------------------------------------
CREATE TABLE IF NOT EXISTS teams (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id         INTEGER NOT NULL REFERENCES seasons(id),
    name              TEXT NOT NULL,
    conference_id     INTEGER NOT NULL REFERENCES conferences(id),
    team_number       INTEGER,          -- schedule draw (1-11), set on draft day
    draft_slot        INTEGER,          -- card draw (1-11), set on draft day
    manager_names     TEXT,             -- 'Scott & Drew'
    passcode_hash     TEXT,             -- set by commissioner / manager
    alive             INTEGER NOT NULL DEFAULT 1,   -- elimination pool
    eliminated_ff_week INTEGER,         -- NULL while alive
    UNIQUE(season_id, name),
    UNIQUE(season_id, conference_id, team_number),
    UNIQUE(season_id, conference_id, draft_slot)
);
CREATE INDEX IF NOT EXISTS ix_teams_season ON teams(season_id, conference_id);

-- ---- NFL universe (cached from nflverse each season) --------------------
CREATE TABLE IF NOT EXISTS nfl_teams (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id    INTEGER NOT NULL REFERENCES seasons(id),
    abbr         TEXT NOT NULL,        -- 'SEA'
    name         TEXT NOT NULL,
    bye_ff_week  INTEGER,              -- the FF week this NFL team is on bye
    UNIQUE(season_id, abbr)
);

CREATE TABLE IF NOT EXISTS nfl_players (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id     INTEGER NOT NULL REFERENCES seasons(id),
    gsis_id       TEXT NOT NULL,       -- joins to nflverse stats
    name          TEXT NOT NULL,
    position      TEXT NOT NULL,       -- RB / WR / TE / ...
    nfl_team_abbr TEXT,
    status        TEXT,
    UNIQUE(season_id, gsis_id)
);
CREATE INDEX IF NOT EXISTS ix_players_pos ON nfl_players(season_id, position);

-- ---- rosters (append-only ownership) ------------------------------------
CREATE TABLE IF NOT EXISTS roster_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id        INTEGER NOT NULL REFERENCES seasons(id),
    team_id          INTEGER NOT NULL REFERENCES teams(id),
    asset_kind       TEXT NOT NULL CHECK (asset_kind IN ('PLAYER','TEAM_UNIT')),
    asset_ref        TEXT NOT NULL,    -- gsis_id or NFL abbr
    unit_type        TEXT CHECK (unit_type IN ('QB','K','DEF/ST','C')),
    roster_slot      TEXT NOT NULL CHECK (roster_slot IN ('C','K','DEF/ST','QB','RB','R')),
    acquired_ff_week INTEGER NOT NULL,
    acquired_via     TEXT NOT NULL CHECK (acquired_via IN ('DRAFT','TRADE')),
    released_ff_week INTEGER,          -- NULL = still on roster
    slot_order       INTEGER,          -- stable within-slot rank; traded-in
                                       -- entries inherit the replaced player's
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_roster_active ON roster_entries(season_id, team_id, released_ff_week);
CREATE INDEX IF NOT EXISTS ix_roster_asset  ON roster_entries(season_id, asset_kind, asset_ref, released_ff_week);

-- ---- weekly lineups (the 9 starters we actually score) ------------------
CREATE TABLE IF NOT EXISTS weekly_lineups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id    INTEGER NOT NULL REFERENCES seasons(id),
    team_id      INTEGER NOT NULL REFERENCES teams(id),
    ff_week      INTEGER NOT NULL,
    roster_slot  TEXT NOT NULL,        -- the slot this start fills (C/K/.../RB/R)
    asset_kind   TEXT NOT NULL,
    asset_ref    TEXT NOT NULL,
    unit_type    TEXT,
    is_rental    INTEGER NOT NULL DEFAULT 0,  -- 1 = filled via an OPEN this week
    submitted_at TEXT,
    UNIQUE(season_id, team_id, ff_week, roster_slot, asset_ref)
);
CREATE INDEX IF NOT EXISTS ix_lineup_week ON weekly_lineups(season_id, ff_week, team_id);

-- ---- transactions (trade = permanent, open = one-week rental) -----------
CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id      INTEGER NOT NULL REFERENCES seasons(id),
    team_id        INTEGER NOT NULL REFERENCES teams(id),
    ff_week        INTEGER NOT NULL,
    type           TEXT NOT NULL CHECK (type IN ('TRADE','OPEN')),
    position       TEXT NOT NULL,      -- like-for-like slot (RB/R/QB/K/DEF-ST/C)
    out_asset_kind TEXT NOT NULL,
    out_asset_ref  TEXT NOT NULL,
    in_asset_kind  TEXT NOT NULL,
    in_asset_ref   TEXT NOT NULL,
    fee_cents      INTEGER NOT NULL DEFAULT 200,
    reversed       INTEGER NOT NULL DEFAULT 0,   -- commissioner can reverse
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tx_team ON transactions(season_id, team_id, reversed);

-- ---- fees / payments (commissioner records payoffs) ---------------------
CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id    INTEGER NOT NULL REFERENCES seasons(id),
    team_id      INTEGER NOT NULL REFERENCES teams(id),
    amount_cents INTEGER NOT NULL,
    note         TEXT,
    applied_by   TEXT,                 -- which admin recorded it
    applied_at   TEXT NOT NULL
);

-- ---- scoring ------------------------------------------------------------
-- Per-asset engine score for a week (box-score line, with breakdown).
CREATE TABLE IF NOT EXISTS asset_week_scores (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id      INTEGER NOT NULL REFERENCES seasons(id),
    ff_week        INTEGER NOT NULL,
    asset_kind     TEXT NOT NULL,
    asset_ref      TEXT NOT NULL,
    unit_type      TEXT,
    points         REAL NOT NULL,
    breakdown_json TEXT,               -- itemized ScoreBreakdown
    computed_at    TEXT NOT NULL,
    -- unit_type MUST be in the key: one NFL team fields 4 distinct units
    -- (QB, K, DEF/ST, C). Players use '' (never NULL) so the dedup key holds.
    UNIQUE(season_id, ff_week, asset_kind, asset_ref, unit_type)
);

-- Per-team weekly total: our computed score + the site's posted score.
CREATE TABLE IF NOT EXISTS team_week_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id       INTEGER NOT NULL REFERENCES seasons(id),
    team_id         INTEGER NOT NULL REFERENCES teams(id),
    ff_week         INTEGER NOT NULL,
    computed_points REAL,
    posted_points   REAL,              -- scraped from legacy site (cross-check)
    finalized       INTEGER NOT NULL DEFAULT 0,
    adjusted        INTEGER NOT NULL DEFAULT 0,  -- 1 = commissioner-overridden total
    computed_at     TEXT,
    UNIQUE(season_id, team_id, ff_week)
);

-- ---- schedule (materialized from the rotation once team_numbers set) ----
CREATE TABLE IF NOT EXISTS matchups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id    INTEGER NOT NULL REFERENCES seasons(id),
    ff_week      INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('INTERLEAGUE','CONFERENCE','BYE','NO_PLAY')),
    home_team_id INTEGER REFERENCES teams(id),
    away_team_id INTEGER REFERENCES teams(id)   -- NULL for BYE/NO_PLAY
);
CREATE INDEX IF NOT EXISTS ix_matchups_week ON matchups(season_id, ff_week);

-- ---- legacy-site snapshots (append-only, for accuracy cross-check) ------
CREATE TABLE IF NOT EXISTS scrape_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at_utc TEXT NOT NULL,
    url            TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    raw_text       TEXT NOT NULL
);

-- ---- key/value settings -------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---- private OT-Blitz chat (Scott + Drew, draft-day back-channel) --------
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_id ON chat_messages(id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if absent. Idempotent."""
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an already-created DB up to the current schema. Idempotent and
    safe to call on every startup (adds columns SQLite can't add via
    CREATE TABLE IF NOT EXISTS on a pre-existing table)."""
    # Tables added after the live DB was created (CREATE TABLE IF NOT EXISTS is
    # a no-op where they already exist).
    conn.execute("CREATE TABLE IF NOT EXISTS chat_messages ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT NOT NULL, "
                 "body TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.commit()

    has_roster = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='roster_entries'").fetchone()
    if has_roster:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(roster_entries)")}
        if "slot_order" not in cols:
            conn.execute("ALTER TABLE roster_entries ADD COLUMN slot_order INTEGER")
            conn.commit()

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='team_week_scores'").fetchone():
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(team_week_scores)")}
        if "adjusted" not in tcols:
            conn.execute("ALTER TABLE team_week_scores ADD COLUMN adjusted INTEGER NOT NULL DEFAULT 0")
            conn.commit()

    # asset_week_scores originally keyed without unit_type, so an NFL team's
    # four units (QB/K/DEF/ST/C) collapsed onto one row. Rebuild if the unique
    # key is missing unit_type; the table is a cache, so re-ingest repopulates.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='asset_week_scores'").fetchone():
        keyed_by_unit = False
        for idx in conn.execute("PRAGMA index_list(asset_week_scores)"):
            if idx["origin"] == "u":
                icols = {r["name"] for r in conn.execute(f"PRAGMA index_info('{idx['name']}')")}
                if "unit_type" in icols:
                    keyed_by_unit = True
        if not keyed_by_unit:
            conn.executescript(
                "DROP TABLE asset_week_scores;\n"
                "CREATE TABLE asset_week_scores (\n"
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                "  season_id INTEGER NOT NULL REFERENCES seasons(id),\n"
                "  ff_week INTEGER NOT NULL, asset_kind TEXT NOT NULL,\n"
                "  asset_ref TEXT NOT NULL, unit_type TEXT, points REAL NOT NULL,\n"
                "  breakdown_json TEXT, computed_at TEXT NOT NULL,\n"
                "  UNIQUE(season_id, ff_week, asset_kind, asset_ref, unit_type));")
            conn.commit()


# The 22 teams, by conference (names from the league site).
TEAMS_BY_CONF = {
    "BLUE": ["BarnBurners", "SteelCurtain", "MuddyChicks", "Ralph", "Hellman",
             "BGH", "Pike", "LowRiders", "OT Blitz", "Shuffling Crew", "TightEnds"],
    "RED": ["Smith", "Cooper", "Ribears", "Juggernuts", "TallBears", "Mooners",
            "BigDitkas", "Eddy's Pats", "D&D", "TTS", "Refs"],
}


def seed_reference(conn: sqlite3.Connection, year: int = 2026,
                   label: str = "2026-27") -> int:
    """Seed conferences, the season, the 22 teams, and the two commissioners
    if they don't already exist. Returns the season id. Idempotent.

    team_number, draft_slot, and passcode_hash are left NULL — they're set on
    draft day / at manager onboarding.
    """
    for code, name in (("BLUE", "Blue Conference"), ("RED", "Red Conference")):
        conn.execute("INSERT OR IGNORE INTO conferences(code, name) VALUES (?, ?)",
                     (code, name))

    conn.execute(
        "INSERT OR IGNORE INTO seasons(year, label, current_ff_week, status) "
        "VALUES (?, ?, 0, 'setup')", (year, label))
    season_id = conn.execute("SELECT id FROM seasons WHERE year=?", (year,)).fetchone()["id"]

    conf_ids = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM conferences")}
    for code, names in TEAMS_BY_CONF.items():
        for nm in names:
            conn.execute(
                "INSERT OR IGNORE INTO teams(season_id, name, conference_id) "
                "VALUES (?, ?, ?)", (season_id, nm, conf_ids[code]))

    for admin in ("Steve", "Scott"):
        exists = conn.execute("SELECT 1 FROM admins WHERE name=?", (admin,)).fetchone()
        if not exists:
            conn.execute("INSERT INTO admins(name, created_at) VALUES (?, ?)",
                         (admin, _now()))

    conn.commit()
    return season_id
