"""Tests for matchups, standings/tiebreakers, team scoring, and elimination."""

import pytest

from joyce_ff.league import schema, scoring, standings


@pytest.fixture()
def db():
    c = schema.connect(":memory:")
    schema.init_db(c)
    sid = schema.seed_reference(c)
    return c, sid


def _assign_team_numbers(conn, sid):
    for conf in ("BLUE", "RED"):
        rows = conn.execute(
            "SELECT t.id FROM teams t JOIN conferences c ON c.id=t.conference_id "
            "WHERE t.season_id=? AND c.code=? ORDER BY t.id", (sid, conf)).fetchall()
        for i, r in enumerate(rows, 1):
            conn.execute("UPDATE teams SET team_number=? WHERE id=?", (i, r["id"]))
    conn.commit()


# --- ranking / tiebreakers (pure) ---------------------------------------

def test_rank_head_to_head_breaks_tie():
    ts = [{"team_id": 1, "name": "A", "conf": "BLUE", "wins": 5, "conf_wins": 3, "pf": 100},
          {"team_id": 2, "name": "B", "conf": "BLUE", "wins": 5, "conf_wins": 2, "pf": 120}]
    ranked = standings._rank(ts, results=[(1, 2)])          # A beat B head-to-head
    assert [t["team_id"] for t in ranked] == [1, 2]
    assert ranked[0]["seed"] == 1 and ranked[0]["playoffs"] is True


def test_rank_falls_through_to_conf_record_then_points():
    ts = [{"team_id": 1, "name": "A", "conf": "BLUE", "wins": 5, "conf_wins": 2, "pf": 100},
          {"team_id": 2, "name": "B", "conf": "BLUE", "wins": 5, "conf_wins": 3, "pf": 90}]
    ranked = standings._rank(ts, results=[])                # no H2H -> conf record wins
    assert [t["team_id"] for t in ranked] == [2, 1]


def test_ninth_seed_misses_playoffs():
    ts = [{"team_id": i, "name": f"T{i}", "conf": "BLUE", "wins": 11 - i,
           "conf_wins": 0, "pf": 0} for i in range(1, 12)]
    ranked = standings._rank(ts, results=[])
    assert ranked[7]["playoffs"] is True       # seed 8
    assert ranked[8]["playoffs"] is False      # seed 9


# --- matchup generation --------------------------------------------------

def test_generate_matchups_counts(db):
    conn, sid = db
    _assign_team_numbers(conn, sid)
    n = standings.generate_matchups(conn, sid)
    games = conn.execute("SELECT COUNT(*) c FROM matchups WHERE season_id=? AND away_team_id IS NOT NULL",
                         (sid,)).fetchone()["c"]
    byes = conn.execute("SELECT COUNT(*) c FROM matchups WHERE season_id=? AND kind='BYE'",
                        (sid,)).fetchone()["c"]
    # interleague 11/wk x4 = 44; conference 5/wk x2conf x11wk = 110; byes 2/wk x11 = 22
    assert games == 154 and byes == 22 and n == 176


def test_generate_matchups_requires_team_numbers(db):
    conn, sid = db
    with pytest.raises(ValueError):
        standings.generate_matchups(conn, sid)


# --- team scoring from lineup + asset scores -----------------------------

def test_score_team_week_sums_starters(db):
    conn, sid = db
    otb = conn.execute("SELECT id FROM teams WHERE name='OT Blitz'").fetchone()["id"]
    starters = [("QB", "TEAM_UNIT", "CIN", "QB", 9), ("RB", "PLAYER", "p_a", None, 6),
                ("R", "PLAYER", "p_b", None, 3)]
    for slot, kind, ref, unit, pts in starters:
        conn.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,"
                     "asset_kind,asset_ref,unit_type) VALUES (?,?,5,?,?,?,?)",
                     (sid, otb, slot, kind, ref, unit))
        conn.execute("INSERT INTO asset_week_scores(season_id,ff_week,asset_kind,asset_ref,"
                     "unit_type,points,computed_at) VALUES (?,5,?,?,?,?, 't')",
                     (sid, kind, ref, unit, pts))
    conn.commit()
    scoring.score_team_week(conn, sid, 5)
    total = conn.execute("SELECT computed_points FROM team_week_scores WHERE team_id=? AND ff_week=5",
                         (otb,)).fetchone()["computed_points"]
    assert total == 18


# --- elimination pool ----------------------------------------------------

def test_run_elimination_removes_lowest_survivor(db):
    conn, sid = db
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM teams WHERE season_id=? ORDER BY id LIMIT 3", (sid,))]
    for tid, pts in zip(ids, (80, 55, 70)):     # middle team is lowest
        conn.execute("INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points) "
                     "VALUES (?,?,1,?)", (sid, tid, pts))
    conn.commit()
    out = standings.run_elimination(conn, sid, 1)
    assert out == ids[1]
    row = conn.execute("SELECT alive, eliminated_ff_week FROM teams WHERE id=?", (ids[1],)).fetchone()
    assert row["alive"] == 0 and row["eliminated_ff_week"] == 1
    status = standings.pool_status(conn, sid)
    assert any(t["name"] for t in status["eliminated"])
    # an eliminated team is skipped next week even if it scores lowest
    conn.execute("INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points) "
                 "VALUES (?,?,2,?)", (sid, ids[1], 5))
    for tid, pts in zip((ids[0], ids[2]), (60, 40)):
        conn.execute("INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points) "
                     "VALUES (?,?,2,?)", (sid, tid, pts))
    conn.commit()
    assert standings.run_elimination(conn, sid, 2) == ids[2]   # not the already-dead team
