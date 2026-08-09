"""Tests for the league platform schema: init, seed, and core invariants."""

import sqlite3

import pytest

from joyce_ff.league import schema


@pytest.fixture()
def conn():
    c = schema.connect(":memory:")
    schema.init_db(c)
    yield c
    c.close()


def test_init_creates_all_tables(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("seasons", "conferences", "teams", "nfl_players", "roster_entries",
              "weekly_lineups", "transactions", "payments", "asset_week_scores",
              "team_week_scores", "matchups", "settings"):
        assert t in names


def test_seed_reference_creates_22_teams_two_conf_two_admins(conn):
    season_id = schema.seed_reference(conn)
    assert conn.execute("SELECT COUNT(*) c FROM teams WHERE season_id=?",
                        (season_id,)).fetchone()["c"] == 22
    assert conn.execute("SELECT COUNT(*) c FROM conferences").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM admins").fetchone()["c"] == 2
    # 11 per conference
    per = conn.execute(
        "SELECT conf.code, COUNT(*) c FROM teams t "
        "JOIN conferences conf ON conf.id=t.conference_id GROUP BY conf.code")
    assert {r["code"]: r["c"] for r in per} == {"BLUE": 11, "RED": 11}


def test_seed_is_idempotent(conn):
    schema.seed_reference(conn)
    schema.seed_reference(conn)
    assert conn.execute("SELECT COUNT(*) c FROM teams").fetchone()["c"] == 22
    assert conn.execute("SELECT COUNT(*) c FROM admins").fetchone()["c"] == 2


def test_active_roster_reconstruction(conn):
    """Append-only roster: a traded-away player is inactive after release."""
    sid = schema.seed_reference(conn)
    team = conn.execute("SELECT id FROM teams WHERE name='OT Blitz'").fetchone()["id"]
    # drafted a RB in week 1; traded him away effective week 5
    conn.execute(
        "INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,"
        "roster_slot,acquired_ff_week,acquired_via,released_ff_week,created_at) "
        "VALUES (?,?,'PLAYER','00-0000001','RB',1,'DRAFT',5,?)", (sid, team, "t"))
    # picked up a replacement RB in week 5, still active
    conn.execute(
        "INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,"
        "roster_slot,acquired_ff_week,acquired_via,released_ff_week,created_at) "
        "VALUES (?,?,'PLAYER','00-0000002','RB',5,'TRADE',NULL,?)", (sid, team, "t"))

    def active_at(week):
        return {r["asset_ref"] for r in conn.execute(
            "SELECT asset_ref FROM roster_entries WHERE team_id=? "
            "AND acquired_ff_week<=? AND (released_ff_week IS NULL OR released_ff_week>?)",
            (team, week, week))}

    assert active_at(3) == {"00-0000001"}          # before the trade
    assert active_at(6) == {"00-0000002"}          # after the trade
    # current roster (released IS NULL)
    current = {r["asset_ref"] for r in conn.execute(
        "SELECT asset_ref FROM roster_entries WHERE team_id=? AND released_ff_week IS NULL",
        (team,))}
    assert current == {"00-0000002"}


def test_team_name_unique_per_season(conn):
    sid = schema.seed_reference(conn)
    blue = conn.execute("SELECT id FROM conferences WHERE code='BLUE'").fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO teams(season_id,name,conference_id) VALUES (?,?,?)",
                     (sid, "OT Blitz", blue))
