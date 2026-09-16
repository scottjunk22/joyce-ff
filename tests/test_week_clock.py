"""Which week the site is on — two clocks (commissioner, 2026-09-15).

LINEUP week (lineups, trades, Opens): the next week opens at 6am Central on the
Tuesday after the week's last game. SCOREBOARD week (what the site opens to):
6am Central on the day of the next week's first kickoff — Thursday — so the
week's results stay up, and Thursday's "no lineup" reminders get seen.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from joyce_ff.data_sources import nflverse as nv
from joyce_ff.league import progress, runner, schema, scoring

CT = progress.CT
ET = progress.ET


def at(month, day, hour, minute=0):
    return dt.datetime(2026, month, day, hour, minute, tzinfo=CT)


@pytest.fixture()
def season():
    c = schema.connect(":memory:")
    schema.init_db(c)
    schema.migrate(c)
    c.execute("INSERT INTO seasons(year,label,current_ff_week,ff_start_nfl_week) VALUES (2026,'2026-27',1,1)")
    sid = c.execute("SELECT id FROM seasons").fetchone()["id"]
    cid = c.execute("INSERT INTO conferences(code,name) VALUES ('BLUE','Blue')").lastrowid
    a = c.execute("INSERT INTO teams(season_id,conference_id,name,team_number) VALUES (?,?,'A',1)", (sid, cid)).lastrowid
    b = c.execute("INSERT INTO teams(season_id,conference_id,name,team_number) VALUES (?,?,'B',2)", (sid, cid)).lastrowid
    for wk in (1, 2, 3):
        c.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id,away_team_id) VALUES (?,?,'CONFERENCE',?,?)",
                  (sid, wk, a, b))
    # Week 1: Wed opener .. Monday night 7:15pm CT. Week 2: Thursday 7:15pm .. Monday.
    progress.store_kickoffs(c, sid, 1, at(9, 9, 19, 20), at(9, 14, 19, 15))
    progress.store_kickoffs(c, sid, 2, at(9, 17, 19, 15), at(9, 21, 19, 15))
    progress.store_kickoffs(c, sid, 3, at(9, 24, 19, 15), at(9, 28, 19, 15))
    c.commit()
    return c, sid


def weeks(c, sid, now):
    return progress.lineup_week(c, sid, now), progress.scoreboard_week(c, sid, now)


def test_the_week_just_played_stays_up_until_thursday_morning(season):
    c, sid = season
    assert weeks(c, sid, at(9, 14, 23, 0)) == (1, 1)     # Monday night, week over
    assert weeks(c, sid, at(9, 15, 5, 59)) == (1, 1)
    assert weeks(c, sid, at(9, 15, 6, 0)) == (2, 1)      # Tuesday 6am: Week 2 lineups open
    assert weeks(c, sid, at(9, 16, 12, 0)) == (2, 1)     # Wednesday: scoreboard still Week 1
    assert weeks(c, sid, at(9, 17, 5, 59)) == (2, 1)
    assert weeks(c, sid, at(9, 17, 6, 0)) == (2, 2)      # Thursday 6am: scoreboard moves
    assert weeks(c, sid, at(9, 22, 6, 0)) == (3, 2)      # and on it goes


def test_a_week_ending_sunday_still_opens_tuesday(season):
    c, sid = season
    progress.store_kickoffs(c, sid, 1, at(9, 10, 19, 15), at(9, 13, 19, 20))    # Sunday night last
    assert progress.lineup_opens(at(9, 13, 19, 20)) == at(9, 15, 6, 0)


def test_the_last_week_is_as_far_as_it_goes(season):
    c, sid = season
    assert weeks(c, sid, at(9, 30, 12, 0)) == (3, 3)


def test_a_finished_season_keeps_its_stored_week(season):
    """Last season's archive must not jump to an empty week."""
    c, sid = season
    c.execute("UPDATE seasons SET current_ff_week=2 WHERE id=?", (sid,))
    assert weeks(c, sid, at(12, 20, 12, 0)) == (2, 2)


def test_without_kickoffs_it_uses_the_stored_week(season):
    c, sid = season
    c.execute("DELETE FROM settings WHERE key LIKE 'kickoffs:%'")
    assert weeks(c, sid, at(9, 16, 12, 0)) == (1, 1)


def test_the_site_offers_the_upcoming_week_newest_first(tmp_path, monkeypatch):
    from joyce_ff.webapp import create_app

    path = str(tmp_path / "league.sqlite")
    c = schema.connect(path)
    schema.init_db(c)
    schema.migrate(c)
    c.execute("INSERT INTO seasons(year,label,current_ff_week) VALUES (2026,'2026-27',1)")
    sid = c.execute("SELECT id FROM seasons").fetchone()["id"]
    c.execute("INSERT INTO conferences(code,name) VALUES ('BLUE','Blue')")
    c.commit()
    c.close()
    monkeypatch.setattr(progress, "lineup_week", lambda *a, **k: 2)
    monkeypatch.setattr(progress, "scoreboard_week", lambda *a, **k: 1)
    season = create_app(path).test_client().get("/api/state").get_json()["season"]
    assert (season["week"], season["current"], season["lineup_week"]) == (1, 1, 2)
    assert season["weeks"] == [2, 1]


def test_nflverse_keeps_being_checked_for_a_few_days_after_a_week_is_final(season, monkeypatch):
    c, sid = season
    progress.mark_finalized(c, sid, 1)
    c.commit()
    games = pd.DataFrame([{"season": 2026, "week": 1, "game_id": "G1", "home_team": "X", "away_team": "Y",
                           "home_score": 20.0, "gameday": "2026-09-14", "gametime": "20:15"}])
    monkeypatch.setattr(nv, "load_games", lambda: games)
    calls = []
    monkeypatch.setattr(scoring, "ingest_asset_scores_from_nflverse", lambda conn, s, ff: calls.append(ff))
    monkeypatch.setattr(scoring, "ingest_week", lambda *a, **k: {})
    runner.run_current(c, sid, now=dt.datetime(2026, 9, 15, 12, 0, tzinfo=ET))
    assert calls == [1]
    calls.clear()
    runner.run_current(c, sid, now=dt.datetime(2026, 9, 25, 12, 0, tzinfo=ET))    # long after
    assert calls == []


def test_opens_look_a_week_ahead_only_from_monday_6am(season):
    """An Open is for this week's byes; next week's byes open up the morning
    after Sunday's games, until Tuesday 6am moves the week on (2026-09-16)."""
    c, sid = season
    assert progress.open_weeks(c, sid, at(9, 16, 12, 0)) == [2]      # Wednesday of Week 2
    assert progress.open_weeks(c, sid, at(9, 20, 22, 0)) == [2]      # Sunday night
    assert progress.open_weeks(c, sid, at(9, 21, 6, 0)) == [2, 3]    # Monday 6am
    assert progress.open_weeks(c, sid, at(9, 22, 5, 59)) == [2, 3]
    assert progress.open_weeks(c, sid, at(9, 22, 6, 0)) == [3]       # Tuesday: Week 3 is this week
    assert progress.open_weeks(c, sid, at(9, 28, 6, 0)) == [3]       # no Week 4 to look ahead to
