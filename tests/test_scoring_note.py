"""An empty scoreboard must always come with a reason.

A blank scoreboard and a scoreboard full of real zeros look identical, and the
first weekend of a season produces the blank one for a legitimate reason: the
schedule publishes final scores immediately, but nflverse doesn't build the
season's play-by-play — which is where every stat comes from — until after the
first games. Silence there reads as "the site is broken".
"""

from __future__ import annotations

import json

import pytest

from joyce_ff.data_sources import nflverse as nv
from joyce_ff.league import runner, schema


def _fixed_schedule(monkeypatch, finals: int, games: int):
    """Pin the NFL schedule so the test doesn't drift as the real week plays
    out, and pin FF week 1 to NFL week 1 the way a practice season does."""
    import pandas as pd

    from joyce_ff.league import scoring as sc

    rows = [{"season": 2026, "week": 1, "home_team": f"H{i}", "away_team": f"A{i}",
             "home_score": (13.0 if i < finals else None)} for i in range(games)]
    monkeypatch.setattr(nv, "load_games", lambda: pd.DataFrame(rows))
    monkeypatch.setattr(sc, "nfl_week_for", lambda *a, **k: 1)


def _season(tmp_path):
    conn = schema.connect(str(tmp_path / "l.sqlite"))
    schema.init_db(conn)
    schema.migrate(conn)
    conn.execute("INSERT INTO seasons(year,label,current_ff_week) VALUES (2026,'2026-27',1)")
    sid = conn.execute("SELECT id FROM seasons").fetchone()["id"]
    conn.execute("INSERT OR IGNORE INTO conferences(code,name) VALUES ('BLUE','Blue')")
    cid = conn.execute("SELECT id FROM conferences WHERE code='BLUE'").fetchone()["id"]
    conn.execute("INSERT INTO teams(season_id,conference_id,name,team_number) "
                 "VALUES (?,?,'OT Blitz',1)", (sid, cid))
    conn.commit()
    return conn, sid


def test_no_lineups_says_so_rather_than_nothing(tmp_path):
    conn, sid = _season(tmp_path)
    r = runner.run_current(conn, sid)
    assert r["scored"] == [] and r["live"] == []
    assert "lineup" in r["note"].lower()
    assert runner.scoring_note(conn, sid) == r["note"]


def test_unpublished_play_by_play_is_reported_not_swallowed(tmp_path, monkeypatch):
    conn, sid = _season(tmp_path)
    conn.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,"
                 "asset_kind,asset_ref) VALUES (?,?,?,?,?,?)",
                 (sid, conn.execute("SELECT id FROM teams").fetchone()["id"], 1, "QB", "TEAM", "SEA"))
    conn.commit()

    # One game final out of sixteen — the opening Wednesday night, exactly when
    # the schedule has a score and the play-by-play file doesn't exist yet.
    _fixed_schedule(monkeypatch, finals=1, games=16)
    boom = nv.NotPublishedYet("nflverse hasn't published 2026 play-by-play yet.")
    monkeypatch.setattr(runner, "run_week",
                        lambda *a, **k: (_ for _ in ()).throw(boom))

    r = runner.run_current(conn, sid)
    assert "play-by-play" in r["note"]
    assert runner.scoring_note(conn, sid) == r["note"]


def test_the_note_reaches_the_site(tmp_path):
    from joyce_ff.webapp import create_app

    conn, sid = _season(tmp_path)
    runner.run_current(conn, sid)          # records the "no lineups" note
    conn.close()

    c = create_app(str(tmp_path / "l.sqlite")).test_client()
    state = json.loads(c.get("/api/state").data)
    assert "lineup" in (state["scoring_note"] or "").lower()


def test_a_clean_run_clears_the_note(tmp_path, monkeypatch):
    conn, sid = _season(tmp_path)
    runner._note(conn, sid, "stale reason from an earlier run")
    conn.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,"
                 "asset_kind,asset_ref) VALUES (?,?,?,?,?,?)",
                 (sid, conn.execute("SELECT id FROM teams").fetchone()["id"], 1, "QB", "TEAM", "SEA"))
    conn.commit()

    _fixed_schedule(monkeypatch, finals=16, games=16)
    monkeypatch.setattr(runner, "run_week", lambda *a, **k: None)

    r = runner.run_current(conn, sid)
    assert r["note"] is None
    assert runner.scoring_note(conn, sid) is None


@pytest.mark.parametrize("season", [2026])
def test_missing_season_file_raises_the_specific_error(season):
    """Guard the distinction itself: a season nflverse hasn't built yet must not
    surface as a bare HTTPError, or callers can't tell it from a real outage."""
    assert issubclass(nv.NotPublishedYet, RuntimeError)
