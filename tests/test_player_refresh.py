"""The daily player-pool refresh (commissioner, 2026-09-16)."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from joyce_ff.data_sources import nflverse as nv
from joyce_ff.league import progress, schema, setup


@pytest.fixture()
def pool():
    c = schema.connect(":memory:")
    schema.init_db(c)
    schema.migrate(c)
    c.execute("INSERT INTO seasons(year,label,current_ff_week,ff_start_nfl_week) VALUES (2026,'2026-27',1,3)")
    sid = c.execute("SELECT id FROM seasons").fetchone()["id"]
    for gid, name, pos, team in (("p_moved", "Moved Receiver", "WR", "KC"),
                                 ("p_gone", "Released Back", "RB", "NYJ")):
        c.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr,status) "
                  "VALUES (?,?,?,?,?,'ACT')", (sid, gid, name, pos, team))
    c.commit()
    return c, sid


ROSTER = pd.DataFrame([
    {"gsis_id": "p_moved", "full_name": "Moved Receiver", "position": "WR", "team": "SF", "status": "ACT"},
    {"gsis_id": "p_new", "full_name": "Called Up", "position": "RB", "team": "DAL", "status": "ACT"},
    {"gsis_id": "p_qb", "full_name": "Some Quarterback", "position": "QB", "team": "DAL", "status": "ACT"},
    {"gsis_id": None, "full_name": "No Id", "position": "WR", "team": "DAL", "status": "ACT"},
])


def _players(c):
    return {r["gsis_id"]: r["nfl_team_abbr"] for r in c.execute("SELECT gsis_id, nfl_team_abbr FROM nfl_players")}


def test_a_traded_player_follows_his_new_team_and_a_signing_is_added(pool):
    c, sid = pool
    assert setup.refresh_nfl_players(c, sid, 2026, roster=ROSTER) == {"updated": 1, "added": 1}
    assert _players(c) == {"p_moved": "SF", "p_gone": "NYJ", "p_new": "DAL"}   # nobody removed, no QB
    assert setup.refresh_nfl_players(c, sid, 2026, roster=ROSTER) == {"updated": 0, "added": 0}


def test_it_runs_once_per_central_day(pool, monkeypatch):
    c, sid = pool
    calls = []
    monkeypatch.setattr(nv, "load_roster", lambda year: calls.append(year) or ROSTER)
    at = lambda d, h: dt.datetime(2026, 9, d, h, tzinfo=progress.CT)
    assert setup.refresh_nfl_players_daily(c, sid, 2026, at(16, 1)) is not None
    assert setup.refresh_nfl_players_daily(c, sid, 2026, at(16, 23)) is None
    assert setup.refresh_nfl_players_daily(c, sid, 2026, at(17, 0)) is not None
    assert calls == [2026, 2026]
