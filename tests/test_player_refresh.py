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


# --- adding a player who is on no NFL team (Scott, 2026-10-05) ------------------

def _season(rows):
    return pd.DataFrame([dict(zip(("gsis_id", "full_name", "position", "team", "status"), r))
                         for r in rows])


HISTORY = {
    2026: ROSTER,                                                  # he isn't on a roster this year
    2025: _season([("00-0033040", "Tyreek Hill", "WR", "MIA", "RES"),
                   ("p_w1", "Mike Williams", "WR", "LAC", "ACT")]),
    2024: _season([("00-0033040", "Tyreek Hill", "WR", "MIA", "ACT"),
                   ("p_w2", "Mike Williams", "WR", "PIT", "ACT"),
                   ("p_k", "Tyreek Hill", "K", "DAL", "ACT")]),    # a kicker isn't a player here
    2023: _season([("00-0033040", "Tyreek Hill", "WR", "MIA", "ACT")]),
}
LOAD = HISTORY.__getitem__


def test_an_unsigned_player_goes_in_with_his_real_id_and_no_team(pool):
    c, sid = pool
    p = setup.add_free_agent(c, sid, 2026, "tyreek hill", load=LOAD)
    assert p == {"added": True, "gsis_id": "00-0033040", "name": "Tyreek Hill", "position": "WR",
                 "team": None, "last_team": "MIA", "last_season": 2025}
    row = c.execute("SELECT name, position, nfl_team_abbr, status FROM nfl_players "
                    "WHERE gsis_id='00-0033040'").fetchone()
    assert tuple(row) == ("Tyreek Hill", "WR", None, "FA")         # NOT Miami: he has no game
    # asked twice: still one of him
    assert setup.add_free_agent(c, sid, 2026, "Tyreek Hill", load=LOAD)["added"] is False
    assert c.execute("SELECT COUNT(*) n FROM nfl_players WHERE name='Tyreek Hill'").fetchone()["n"] == 1


def test_when_he_signs_the_daily_refresh_fills_in_his_team_without_a_second_entry(pool):
    c, sid = pool
    setup.add_free_agent(c, sid, 2026, "Tyreek Hill", load=LOAD)
    # until nflverse lists him, the refresh leaves him exactly as he is
    setup.refresh_nfl_players(c, sid, 2026, roster=ROSTER)
    assert _players(c)["00-0033040"] is None
    signed = pd.concat([ROSTER, _season([("00-0033040", "Tyreek Hill", "WR", "KC", "ACT")])])
    assert setup.refresh_nfl_players(c, sid, 2026, roster=signed)["added"] == 0
    rows = c.execute("SELECT nfl_team_abbr, status FROM nfl_players WHERE name='Tyreek Hill'").fetchall()
    assert [tuple(r) for r in rows] == [("KC", "ACT")]


def test_a_name_it_cannot_pin_to_one_player_is_refused(pool):
    c, sid = pool
    with pytest.raises(ValueError, match="no RB, WR or TE matching 'Tyreke Hill'"):
        setup.add_free_agent(c, sid, 2026, "Tyreke Hill", load=LOAD)           # misspelt
    with pytest.raises(ValueError, match="more than one player.*p_w1.*p_w2"):
        setup.add_free_agent(c, sid, 2026, "Mike Williams", load=LOAD)         # two of them
    with pytest.raises(ValueError, match="id 00-9999999"):
        setup.add_free_agent(c, sid, 2026, "Tyreek Hill", gsis_id="00-9999999", load=LOAD)  # made-up id
    assert "p_w1" not in _players(c) and "00-0033040" not in _players(c)       # nobody was added
    assert setup.add_free_agent(c, sid, 2026, "Mike Williams", gsis_id="p_w2", load=LOAD)["added"]
    assert _players(c)["p_w2"] is None
