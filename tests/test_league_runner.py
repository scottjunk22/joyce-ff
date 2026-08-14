"""Tests for the weekly runner, reconcile, and legacy-site scrape/store."""

import pytest

from joyce_ff.league import runner, schema, scrape


@pytest.fixture()
def db():
    c = schema.connect(":memory:")
    schema.init_db(c)
    sid = schema.seed_reference(c)
    return c, sid


def _seed_week(conn, sid, ff_week, team_points):
    """Give each (team, points) a lineup of one asset worth those points."""
    for tid, pts in team_points:
        ref = f"a{tid}"
        conn.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,"
                     "asset_kind,asset_ref) VALUES (?,?,?, 'QB','TEAM_UNIT',?)",
                     (sid, tid, ff_week, ref))
        conn.execute("INSERT INTO asset_week_scores(season_id,ff_week,asset_kind,asset_ref,"
                     "points,computed_at) VALUES (?,?, 'TEAM_UNIT',?,?, 't')",
                     (sid, ff_week, ref, pts))
    conn.commit()


def test_run_week_scores_and_advances_and_is_idempotent(db):
    conn, sid = db
    ids = [r["id"] for r in conn.execute("SELECT id FROM teams WHERE season_id=? ORDER BY id LIMIT 3", (sid,))]
    _seed_week(conn, sid, 1, list(zip(ids, (70, 40, 60))))     # middle is lowest
    s = runner.run_week(conn, sid, 1, do_ingest=False)
    assert s["team_scores"][ids[1]] == 40
    assert s["eliminated_team_ids"] == [ids[1]]
    assert conn.execute("SELECT current_ff_week FROM seasons WHERE id=?", (sid,)).fetchone()[0] == 1
    # re-run must NOT eliminate a second team
    s2 = runner.run_week(conn, sid, 1, do_ingest=False)
    assert s2["eliminated_team_ids"] == [ids[1]]
    assert conn.execute("SELECT COUNT(*) c FROM teams WHERE season_id=? AND alive=0", (sid,)).fetchone()["c"] == 1


def test_tie_for_lowest_eliminates_all_tied_teams(db):
    conn, sid = db
    ids = [r["id"] for r in conn.execute("SELECT id FROM teams WHERE season_id=? ORDER BY id LIMIT 3", (sid,))]
    _seed_week(conn, sid, 1, list(zip(ids, (70, 40, 40))))     # two teams tie for lowest
    s = runner.run_week(conn, sid, 1, do_ingest=False)
    assert set(s["eliminated_team_ids"]) == {ids[1], ids[2]}   # both go, no tiebreak
    assert conn.execute("SELECT COUNT(*) c FROM teams WHERE season_id=? AND alive=0",
                        (sid,)).fetchone()["c"] == 2


def test_reconcile_flags_mismatch(db):
    conn, sid = db
    ids = [r["id"] for r in conn.execute("SELECT id FROM teams WHERE season_id=? ORDER BY id LIMIT 2", (sid,))]
    conn.execute("INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points,posted_points) "
                 "VALUES (?,?,1,44,44)", (sid, ids[0]))          # match
    conn.execute("INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points,posted_points) "
                 "VALUES (?,?,1,50,47)", (sid, ids[1]))          # mismatch
    conn.commit()
    rec = runner.reconcile_week(conn, sid, 1)
    assert rec["checked"] == 2 and rec["matched"] == 1
    assert rec["mismatches"][0]["team"] and rec["mismatches"][0]["computed"] == 50


def test_carry_forward_copies_previous_lineup(db):
    conn, sid = db
    tid = conn.execute("SELECT id FROM teams WHERE season_id=? LIMIT 1", (sid,)).fetchone()["id"]
    conn.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,asset_ref) "
                 "VALUES (?,?,3,'QB','TEAM_UNIT','KC')", (sid, tid))
    conn.commit()
    n = runner.carry_forward_lineups(conn, sid, 4)        # week 4 has no lineup
    assert n == 1
    got = conn.execute("SELECT asset_ref FROM weekly_lineups WHERE team_id=? AND ff_week=4",
                       (tid,)).fetchall()
    assert [r["asset_ref"] for r in got] == ["KC"]
    # idempotent: doesn't duplicate if a lineup already exists
    assert runner.carry_forward_lineups(conn, sid, 4) == 0


SITE_FIXTURE = """
<table>
  <tr><td>#</td><td>#5 Pike</td></tr>
  <tr><td>C</td><td>NE 0</td></tr>
  <tr><td>QB</td><td>Seatt 3</td></tr>
  <tr><td>Total</td><td>100</td></tr>
</table>
"""


def test_scrape_stores_posted_total_and_snapshot(db):
    conn, sid = db
    r = scrape.scrape_and_store(conn, sid, ff_week=5, html=SITE_FIXTURE)
    assert r["matched"] == 1
    pike = conn.execute("SELECT id FROM teams WHERE name='Pike'").fetchone()["id"]
    posted = conn.execute("SELECT posted_points FROM team_week_scores WHERE team_id=? AND ff_week=5",
                          (pike,)).fetchone()["posted_points"]
    assert posted == 100
    assert conn.execute("SELECT COUNT(*) c FROM scrape_snapshots").fetchone()["c"] == 1
