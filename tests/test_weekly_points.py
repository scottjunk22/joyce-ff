"""The commissioner's Weekly points list.

Every team unit and every RB/receiver with a stat line, for any week, with who
held each that week — so the commissioner can answer "how much did Indianapolis
DEF/ST get?" even when nobody owns them. Commissioner only.
"""

from __future__ import annotations

import json

import pytest

from joyce_ff.league import auth, progress, repo, schema

NFL = [("IND", None), ("KC", None), ("BAL", None), ("PIT", None), ("CIN", None),
       ("T1", None), ("T2", None), ("T3", 2), ("T8", None)]
PLAYERS = [("r1", "RB", "T1"), ("r2", "RB", "T2"), ("r3", "RB", "T3"),
           ("w1", "WR", "T1"), ("w2", "WR", "T2"), ("w3", "WR", "T1"), ("w4", "TE", "T2"),
           ("fa_r", "WR", "T8"), ("fa_rb", "RB", "T8")]
ROSTER = [("TEAM_UNIT", "KC", "C"), ("TEAM_UNIT", "BAL", "K"), ("TEAM_UNIT", "PIT", "DEF/ST"),
          ("TEAM_UNIT", "CIN", "QB"), ("PLAYER", "r1", "RB"), ("PLAYER", "r2", "RB"),
          ("PLAYER", "r3", "RB"), ("PLAYER", "w1", "R"), ("PLAYER", "w2", "R"),
          ("PLAYER", "w3", "R"), ("PLAYER", "w4", "R")]


@pytest.fixture()
def site(tmp_path):
    from joyce_ff.webapp import create_app

    path = str(tmp_path / "league.sqlite")
    c = schema.connect(path)
    schema.init_db(c)
    schema.migrate(c)
    sid = schema.seed_reference(c)
    c.execute("UPDATE seasons SET current_ff_week=2, ff_start_nfl_week=3 WHERE id=?", (sid,))
    for abbr, bye in NFL:
        c.execute("INSERT INTO nfl_teams(season_id,abbr,name,bye_ff_week) VALUES (?,?,?,?)",
                  (sid, abbr, abbr, bye))
    for gid, pos, team in PLAYERS:
        c.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr) "
                  "VALUES (?,?,?,?,?)", (sid, gid, gid.upper(), pos, team))
    otb = c.execute("SELECT id FROM teams WHERE name='OT Blitz'").fetchone()["id"]
    for kind, ref, slot in ROSTER:
        c.execute("INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,unit_type,"
                  "roster_slot,acquired_ff_week,acquired_via,created_at) VALUES (?,?,?,?,?,?,1,'DRAFT','t')",
                  (sid, otb, kind, ref, slot if kind == "TEAM_UNIT" else None, slot))
    # The same unit owned in the other conference too (separate draft pools).
    red = c.execute("SELECT id FROM teams WHERE name='Cooper'").fetchone()["id"]
    c.execute("INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,unit_type,roster_slot,"
              "acquired_ff_week,acquired_via,created_at) VALUES (?,?,'TEAM_UNIT','PIT','DEF/ST','DEF/ST',1,'DRAFT','t')",
              (sid, red))
    for wk, kind, ref, unit, pts in ((1, "TEAM_UNIT", "IND", "DEF/ST", 9), (1, "PLAYER", "w1", "", 8),
                                     (2, "PLAYER", "w1", "", 5), (2, "PLAYER", "fa_r", "", 6)):
        c.execute("INSERT INTO asset_week_scores(season_id,ff_week,asset_kind,asset_ref,unit_type,points,"
                  "breakdown_json,computed_at) VALUES (?,?,?,?,?,?,?,'t')",
                  (sid, wk, kind, ref, unit, pts, json.dumps([["test", pts]])))
    for wk in (1, 2):
        for home in ("IND", "T1", "T8"):
            c.execute("INSERT INTO nfl_game_locks(season_id,ff_week,game_id,home_team,away_team,locked_at) "
                      "VALUES (?,?,?,?,'X','t')", (sid, wk, f"G{wk}{home}", home))
    progress.mark_live(c, sid, 1)
    progress.mark_live(c, sid, 2)
    repo.do_trade(c, sid, otb, "R", "w2", "fa_r", 2)          # w2 out, fa_r in, from week 2
    repo.do_open(c, sid, otb, "RB", "r3", "fa_rb", 2)          # r3 on bye wk 2: fa_rb rented
    c.execute("INSERT INTO asset_week_scores(season_id,ff_week,asset_kind,asset_ref,unit_type,points,"
              "breakdown_json,computed_at) VALUES (?,2,'PLAYER','fa_rb','',4,'[]','t')", (sid,))
    auth.set_admin_passcode(c, "Steve", "commish")
    c.commit()
    c.close()
    client = create_app(path).test_client()

    def ask(week, passcode="commish"):
        return client.post("/api/admin/weekly-points", json={"passcode": passcode, "week": week})
    return ask


def _row(rows, name):
    return next(r for r in rows if r["name"] == name)


def test_it_is_commissioner_only(site):
    assert site(1, passcode="wrong").status_code in (401, 403)


def test_every_team_unit_is_listed_even_when_nobody_owns_it(site):
    rows = site(1).get_json()["rows"]
    ind = _row(rows, "IND DEF/ST")
    assert (ind["points"], ind["owners"]) == (9.0, [])
    assert len([r for r in rows if r["slot"] == "DEF/ST"]) == len(NFL)


def test_players_are_listed_only_with_a_stat_line(site):
    names = {r["name"] for r in site(1).get_json()["rows"] if r["slot"] in ("RB", "R")}
    assert names == {"W1"}


def test_owned_by_follows_trades_week_by_week(site):
    wk1, wk2 = site(1).get_json()["rows"], site(2).get_json()["rows"]
    assert [o["team"] for o in _row(wk1, "W1")["owners"]] == ["OT Blitz"]
    assert [o["team"] for o in _row(wk2, "FA_R")["owners"]] == ["OT Blitz"]   # traded in, week 2


def test_a_unit_owned_in_both_conferences_shows_both(site):
    owners = _row(site(1).get_json()["rows"], "PIT DEF/ST")["owners"]
    assert {(o["team"], o["conf"]) for o in owners} == {("OT Blitz", "BLUE"), ("Cooper", "RED")}


def test_points_wait_for_a_final_game_and_a_bye_is_zero(site):
    rows = site(2).get_json()["rows"]
    assert _row(rows, "PIT DEF/ST")["points"] is None          # PIT's game isn't locked
    bye = _row(rows, "T3 DEF/ST")
    assert (bye["state"], bye["points"]) == ("bye", 0.0)


def test_a_rental_is_marked_open_with_who_it_covered(site):
    wk2 = site(2).get_json()["rows"]
    (o,) = _row(wk2, "FA_RB")["owners"]
    assert (o["team"], o["open"], o["covering"]) == ("OT Blitz", True, "R3")
    assert _row(site(1).get_json()["rows"], "W1")["owners"][0]["open"] is False
