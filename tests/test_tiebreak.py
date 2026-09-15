"""Breaking a tied game: fewest DEF/ST net yards allowed (commissioner, 2026-09-14).

2026 Week 1's Ralph vs Smith tied 44-44: Ralph started Minnesota (419 yards
allowed), Smith started Philadelphia (295) — Smith wins. A DEF/ST on bye that
wasn't covered by an Open loses; both on bye, or equal yards, is the
commissioner's call. There are no ties in the W-L column.
"""

from __future__ import annotations

import pytest

from joyce_ff.league import auth, progress, schema, scoring, tiebreak
from joyce_ff.league import standings as st


@pytest.fixture()
def league(tmp_path):
    path = str(tmp_path / "league.sqlite")
    c = schema.connect(path)
    schema.init_db(c)
    schema.migrate(c)
    c.execute("INSERT INTO seasons(year,label,current_ff_week,ff_start_nfl_week) VALUES (2026,'2026-27',1,1)")
    sid = c.execute("SELECT id FROM seasons").fetchone()["id"]
    cid = c.execute("INSERT INTO conferences(code,name) VALUES ('BLUE','Blue')").lastrowid
    ralph = c.execute("INSERT INTO teams(season_id,conference_id,name,team_number) VALUES (?,?,'Ralph',1)", (sid, cid)).lastrowid
    smith = c.execute("INSERT INTO teams(season_id,conference_id,name,team_number) VALUES (?,?,'Smith',2)", (sid, cid)).lastrowid
    c.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id,away_team_id) VALUES (?,1,'CONFERENCE',?,?)",
              (sid, ralph, smith))
    for abbr, bye in (("MIN", None), ("PHI", None), ("BYE", 1), ("DAL", None)):
        c.execute("INSERT INTO nfl_teams(season_id,abbr,name,bye_ff_week) VALUES (?,?,?,?)", (sid, abbr, abbr, bye))
    for tid in (ralph, smith):
        c.execute("INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points) VALUES (?,?,1,44)", (sid, tid))
    c.commit()

    def start(team, defense, yards=None, rental=False):
        c.execute("DELETE FROM weekly_lineups WHERE team_id=? AND roster_slot='DEF/ST'", (team,))
        c.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,asset_ref,"
                  "unit_type,is_rental) VALUES (?,?,1,'DEF/ST','TEAM_UNIT',?,'DEF/ST',?)",
                  (sid, team, defense, int(rental)))
        c.execute("INSERT OR IGNORE INTO asset_week_scores(season_id,ff_week,asset_kind,asset_ref,unit_type,"
                  "points,computed_at,yards_allowed) VALUES (?,1,'TEAM_UNIT',?,'DEF/ST',0,'t',?)",
                  (sid, defense, yards))
        c.commit()

    return c, sid, ralph, smith, start, path


def _record(c, sid, team):
    t = next(t for t in st.compute_standings(c, sid)["BLUE"] if t["team_id"] == team)
    return t["wins"], t["losses"]


def test_fewest_yards_allowed_wins(league):
    c, sid, ralph, smith, start, _ = league
    start(ralph, "MIN", 419)
    start(smith, "PHI", 295)
    d = tiebreak.decide(c, sid, 1, ralph, smith)
    assert d["winner"] == smith and not d["needs_commissioner"]
    assert d["text"] == "Smith wins tiebreaker: 295 yards allowed vs 419"
    assert _record(c, sid, smith) == (1, 0) and _record(c, sid, ralph) == (0, 1)


def test_a_defense_on_bye_loses(league):
    c, sid, ralph, smith, start, _ = league
    start(ralph, "BYE")
    start(smith, "PHI", 450)                     # even a bad day beats not playing
    d = tiebreak.decide(c, sid, 1, ralph, smith)
    assert d["winner"] == smith and "Ralph's DEF/ST was on bye" in d["text"]


def test_a_rented_defense_counts(league):
    c, sid, ralph, smith, start, _ = league
    start(ralph, "DAL", 250, rental=True)        # Ralph Opened Dallas for his bye defense
    start(smith, "PHI", 295)
    assert tiebreak.decide(c, sid, 1, ralph, smith)["winner"] == ralph


@pytest.mark.parametrize("ralph_def,smith_def", [(("BYE", None), ("BYE", None)),
                                                 (("MIN", 300), ("PHI", 300))])
def test_both_on_bye_or_equal_yards_is_the_commissioners_call(league, ralph_def, smith_def):
    c, sid, ralph, smith, start, _ = league
    start(ralph, *ralph_def)
    if smith_def[0] != ralph_def[0]:
        start(smith, *smith_def)
    else:
        start(smith, smith_def[0])
    d = tiebreak.decide(c, sid, 1, ralph, smith)
    assert d["winner"] is None and d["needs_commissioner"]
    assert _record(c, sid, ralph) == (0, 0) and _record(c, sid, smith) == (0, 0)


def test_the_commissioners_decision_counts(league):
    from joyce_ff.webapp import create_app

    c, sid, ralph, smith, start, path = league
    start(ralph, "MIN", 300)
    start(smith, "PHI", 300)
    c.execute("INSERT OR IGNORE INTO admins(name,created_at) VALUES ('Steve','t')")
    auth.set_admin_passcode(c, "Steve", "commish")
    c.commit()
    client = create_app(path).test_client()
    state = client.get("/api/state?week=1").get_json()
    assert [(t["home"], t["away"]) for t in state["ties_to_decide"]] == [("Ralph", "Smith")]
    r = client.post("/api/admin/tiebreak", json={"passcode": "commish", "week": 1, "home": ralph,
                                                  "away": smith, "winner": ralph})
    assert r.status_code == 200
    assert _record(c, sid, ralph) == (1, 0)
    assert client.get("/api/state?week=1").get_json()["ties_to_decide"] == []
    assert client.post("/api/admin/tiebreak", json={"passcode": "nope", "week": 1, "home": ralph,
                                                    "away": smith, "winner": ralph}).status_code == 403


def test_missing_yards_waits_rather_than_guessing(league):
    c, sid, ralph, smith, start, _ = league
    start(ralph, "MIN", None)
    start(smith, "PHI", 295)
    d = tiebreak.decide(c, sid, 1, ralph, smith)
    assert d["winner"] is None and not d["needs_commissioner"]


def test_filling_yards_never_changes_a_score(league):
    c, sid, ralph, smith, start, _ = league
    start(ralph, "MIN", None)
    c.execute("UPDATE asset_week_scores SET points=6 WHERE asset_ref='MIN'")
    assert scoring.fill_yards_allowed(c, sid, 1, "MIN", 419) == 1
    assert scoring.fill_yards_allowed(c, sid, 1, "MIN", 999) == 0          # only blanks
    row = c.execute("SELECT points, yards_allowed FROM asset_week_scores WHERE asset_ref='MIN'").fetchone()
    assert (row["points"], row["yards_allowed"]) == (6, 419)
