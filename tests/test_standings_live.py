"""A week that's still being played shows scores but decides nothing.

The hourly runner scores a week as its NFL games finish, so totals climb
through the weekend. Before this, those running totals flowed straight into
the standings: a team up 40-12 at 4pm Sunday was already credited with a win,
and a mid-Sunday Week 15 would have shown someone winning the $100 pot.
"""

from __future__ import annotations

import pandas as pd

from joyce_ff.data_sources import nflverse as nv
from joyce_ff.league import runner, schema
from joyce_ff.league import scoring as sc
from joyce_ff.league import standings as st


def _league(tmp_path, week=1, a_pts=50.0, b_pts=40.0):
    conn = schema.connect(str(tmp_path / "l.sqlite"))
    schema.init_db(conn)
    schema.migrate(conn)
    conn.execute("INSERT INTO seasons(year,label,current_ff_week) VALUES (2026,'2026-27',1)")
    sid = conn.execute("SELECT id FROM seasons").fetchone()["id"]
    conn.execute("INSERT OR IGNORE INTO conferences(code,name) VALUES ('BLUE','Blue')")
    cid = conn.execute("SELECT id FROM conferences WHERE code='BLUE'").fetchone()["id"]
    ids = []
    for i, name in enumerate(("Aces", "Bees"), start=1):
        conn.execute("INSERT INTO teams(season_id,conference_id,name,team_number) VALUES (?,?,?,?)",
                     (sid, cid, name, i))
        ids.append(conn.execute("SELECT id FROM teams WHERE name=?", (name,)).fetchone()["id"])
    a, b = ids
    conn.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id,away_team_id) "
                 "VALUES (?,?,'CONFERENCE',?,?)", (sid, week, a, b))
    for tid, pts in ((a, a_pts), (b, b_pts)):
        conn.execute("INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points) "
                     "VALUES (?,?,?,?)", (sid, tid, week, pts))
    conn.commit()
    return conn, sid, a, b


def _row(stand, team_id):
    return next(t for t in stand["BLUE"] if t["team_id"] == team_id)


def test_an_in_progress_week_changes_no_records(tmp_path):
    conn, sid, a, b = _league(tmp_path)
    st.mark_live(conn, sid, 1)
    stand = st.compute_standings(conn, sid)
    for tid in (a, b):
        r = _row(stand, tid)
        assert (r["wins"], r["losses"], r["ties"], r["pf"], r["pa"]) == (0, 0, 0, 0.0, 0.0)


def test_a_final_week_counts(tmp_path):
    conn, sid, a, b = _league(tmp_path)
    st.mark_live(conn, sid, 1)
    st.clear_live(conn, sid, 1)
    stand = st.compute_standings(conn, sid)
    assert (_row(stand, a)["wins"], _row(stand, a)["pf"]) == (1, 50.0)
    assert _row(stand, b)["losses"] == 1


def test_weeks_scored_before_the_flag_existed_still_count(tmp_path):
    """The real 2025-26 season was scored with no flags at all. It must not
    lose its standings because of this change."""
    conn, sid, a, _ = _league(tmp_path)
    assert _row(st.compute_standings(conn, sid), a)["wins"] == 1


def test_no_payout_while_the_final_week_is_being_played(tmp_path):
    conn, sid, _, _ = _league(tmp_path, week=st.FINAL_WEEK)
    st.mark_live(conn, sid, st.FINAL_WEEK)
    assert st.final_payout(conn, sid) is None
    st.clear_live(conn, sid, st.FINAL_WEEK)
    assert st.final_payout(conn, sid) is not None


def test_the_runner_flags_a_live_week_and_unflags_it_when_final(tmp_path, monkeypatch):
    conn, sid, a, _ = _league(tmp_path)
    conn.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,asset_ref) "
                 "VALUES (?,?,1,'QB','TEAM','SEA')", (sid, a))
    conn.commit()
    # Real run_week, but no network: stats ingest and team scoring are no-ops,
    # so the scores already in the table are what standings see.
    monkeypatch.setattr(sc, "ingest_asset_scores_from_nflverse", lambda *a, **k: 0)
    monkeypatch.setattr(sc, "score_team_week", lambda *a, **k: None)
    monkeypatch.setattr(sc, "nfl_week_for", lambda *a, **k: 1)

    def schedule(finals, games=2):
        rows = [{"season": 2026, "week": 1, "home_team": f"H{i}", "away_team": f"A{i}",
                 "home_score": (13.0 if i < finals else None)} for i in range(games)]
        monkeypatch.setattr(nv, "load_games", lambda: pd.DataFrame(rows))

    schedule(finals=1)                       # Sunday afternoon: one game done
    assert runner.run_current(conn, sid)["live"] == [1]
    assert 1 in st.live_weeks(conn, sid)
    assert _row(st.compute_standings(conn, sid), a)["wins"] == 0

    schedule(finals=2)                       # Monday night: all done
    assert runner.run_current(conn, sid)["scored"] == [1]
    assert 1 not in st.live_weeks(conn, sid)
    assert _row(st.compute_standings(conn, sid), a)["wins"] == 1
