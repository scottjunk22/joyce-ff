"""Per-game scoring locks, matchup-by-matchup results, early elimination, and
carrying a lineup forward for a manager who didn't set one.

The rules these encode (commissioner, 2026-09-10):
  * a game's stats lock the first time they're complete and never move again —
    he scores from the box score he saw, not from later corrections;
  * a matchup is final, and on the records, once every starter's game is
    locked; the week's elimination is called as soon as it's certain;
  * an unset lineup carries forward last week's at the week's first kickoff,
    fixed the way he'd fix it by hand, and it's then locked like any other.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from joyce_ff.data_sources import nflverse as nv
from joyce_ff.league import carry, progress, repo, runner, schema, scoring
from joyce_ff.league import standings as st

ET = progress.ET


def _mini_season(names=("Aces", "Bees", "Cats"), path=":memory:"):
    """A season with only these teams (all alive), FF week 1 = NFL week 1."""
    conn = schema.connect(path)
    schema.init_db(conn)
    schema.migrate(conn)
    conn.execute("INSERT INTO seasons(year,label,current_ff_week,ff_start_nfl_week) "
                 "VALUES (2026,'2026-27',1,1)")
    sid = conn.execute("SELECT id FROM seasons").fetchone()["id"]
    conn.execute("INSERT OR IGNORE INTO conferences(code,name) VALUES ('BLUE','Blue')")
    cid = conn.execute("SELECT id FROM conferences WHERE code='BLUE'").fetchone()["id"]
    ids = []
    for i, n in enumerate(names, start=1):
        conn.execute("INSERT INTO teams(season_id,conference_id,name,team_number) VALUES (?,?,?,?)",
                     (sid, cid, n, i))
        ids.append(conn.execute("SELECT id FROM teams WHERE name=?", (n,)).fetchone()["id"])
    conn.commit()
    return conn, sid, ids


# --- per-game locks ---------------------------------------------------------

def _feed(monkeypatch, *, yards, finished, posted=None):
    """Fake nflverse for one game, SEA v NE, with one SEA runner."""
    posted = finished if posted is None else posted
    pbp = pd.DataFrame([{"season_type": "REG", "week": 1, "game_id": "G1",
                         "desc": "END GAME" if finished else "Run up the middle"}])
    games = pd.DataFrame([{"season": 2026, "week": 1, "game_id": "G1",
                           "home_team": "SEA", "away_team": "NE",
                           "home_score": 13.0 if posted else float("nan"),
                           "away_score": 10.0 if posted else float("nan")}])
    player = {"week": 1, "player_id": "p_rb", "name": "Runner", "team": "SEA",
              "rushing_yards": yards, "rushing_tds": 0, "receiving_yards": 0,
              "receptions": 0, "receiving_tds": 0, "return_tds": 0}

    def none(*cols):
        return pd.DataFrame(columns=["week", "team", *cols])

    monkeypatch.setattr(nv, "load_pbp", lambda season: pbp)
    monkeypatch.setattr(nv, "load_games", lambda: games)
    monkeypatch.setattr(nv, "player_week_stats", lambda p: pd.DataFrame([player]))
    monkeypatch.setattr(nv, "qb_unit_week_stats", lambda p: none("passing_yards", "passing_tds"))
    monkeypatch.setattr(nv, "kicker_unit_week_stats",
                        lambda p: none("fg_distances", "extra_points_made"))
    monkeypatch.setattr(nv, "defense_unit_week_stats", lambda p, g, y: none())
    monkeypatch.setattr(nv, "coach_unit_week_stats", lambda g, y: none("won", "tied"))


def _runner_points(conn, sid):
    return conn.execute("SELECT points FROM asset_week_scores WHERE season_id=? "
                        "AND asset_ref='p_rb'", (sid,)).fetchone()["points"]


def test_a_game_in_progress_keeps_updating(monkeypatch):
    conn, sid, _ = _mini_season()
    _feed(monkeypatch, yards=40, finished=False)
    scoring.ingest_asset_scores_from_nflverse(conn, sid, 1)
    early = _runner_points(conn, sid)
    _feed(monkeypatch, yards=120, finished=False)
    scoring.ingest_asset_scores_from_nflverse(conn, sid, 1)
    assert _runner_points(conn, sid) > early
    assert progress.locked_teams(conn, sid, 1) == set()


def test_a_finished_game_locks_and_ignores_later_corrections(monkeypatch):
    conn, sid, _ = _mini_season()
    _feed(monkeypatch, yards=120, finished=True)
    scoring.ingest_asset_scores_from_nflverse(conn, sid, 1)
    as_seen = _runner_points(conn, sid)
    assert as_seen > 0
    assert progress.locked_teams(conn, sid, 1) == {"SEA", "NE"}

    _feed(monkeypatch, yards=40, finished=True)            # Monday's stat correction
    scoring.ingest_asset_scores_from_nflverse(conn, sid, 1)
    assert _runner_points(conn, sid) == as_seen


def test_a_game_waits_for_its_final_score_before_locking(monkeypatch):
    """The coach's win and the defense's points allowed come from the final
    score, so play-by-play reaching END GAME isn't enough on its own."""
    conn, sid, _ = _mini_season()
    _feed(monkeypatch, yards=120, finished=True, posted=False)
    scoring.ingest_asset_scores_from_nflverse(conn, sid, 1)
    assert progress.locked_teams(conn, sid, 1) == set()


# --- done, floors, records, early elimination --------------------------------

def _starters(conn, sid, tid, nfl, each, later=None, n_later=0, wk=1):
    """A full 9-man lineup for NFL team `nfl`, every starter already scored
    `each`. The last `n_later` starters play for `later` instead and haven't
    scored yet (their game is still to come)."""
    slots = ["C", "K", "DEF/ST", "QB", "RB", "RB", "R", "R", "R"]
    for i, slot in enumerate(slots):
        team, pts = (later, 0.0) if later and i >= len(slots) - n_later else (nfl, each)
        if slot in ("RB", "R"):
            ref, kind, unit = f"p{tid}_{i}", "PLAYER", ""
            conn.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr) "
                         "VALUES (?,?,?,?,?)", (sid, ref, ref, "RB" if slot == "RB" else "WR", team))
        else:
            ref, kind, unit = team, "TEAM_UNIT", slot
        conn.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,"
                     "asset_ref,unit_type) VALUES (?,?,?,?,?,?,?)",
                     (sid, tid, wk, slot, kind, ref, unit or None))
        conn.execute("INSERT OR IGNORE INTO asset_week_scores(season_id,ff_week,asset_kind,"
                     "asset_ref,unit_type,points,computed_at) VALUES (?,?,?,?,?,?,'t')",
                     (sid, wk, kind, ref, unit, pts))
    conn.commit()


def _lock(conn, sid, nfl, wk=1):
    conn.execute("INSERT INTO nfl_game_locks(season_id,ff_week,game_id,home_team,away_team,"
                 "locked_at) VALUES (?,?,?,?,?,'t')", (sid, wk, f"G_{nfl}", nfl, f"OPP_{nfl}"))
    conn.commit()


def _in_progress(conn, sid, wk=1):
    """Mark the week as being played, with its last kickoff still ahead."""
    now = dt.datetime.now(ET)
    progress.mark_live(conn, sid, wk)
    progress.store_kickoffs(conn, sid, wk, now - dt.timedelta(days=1), now + dt.timedelta(days=1))
    conn.commit()


def test_a_team_is_done_once_every_starter_is_locked():
    conn, sid, (a, b, _) = _mini_season()
    _starters(conn, sid, a, "AAA", 3)
    _starters(conn, sid, b, "BBB", 5)
    _in_progress(conn, sid)
    _lock(conn, sid, "AAA")
    s = progress.statuses(conn, sid, 1)
    assert s[a]["done"] and s[a]["floor"] == 27
    assert not s[b]["done"] and s[b]["to_play"] == 9 and s[b]["floor"] == 0


def test_a_starter_on_bye_keeps_a_team_open_until_the_last_kickoff():
    """He has no game, so he never locks — and until the last game of the week
    kicks off he could still be swapped out or covered by an Open."""
    conn, sid, (a, _, _) = _mini_season()
    _starters(conn, sid, a, "AAA", 3)
    conn.execute("INSERT INTO nfl_teams(season_id,abbr,name,bye_ff_week) VALUES (?,'BYE','Bye',1)",
                 (sid,))
    conn.execute("UPDATE nfl_players SET nfl_team_abbr='BYE' WHERE gsis_id=?", (f"p{a}_4",))
    progress.mark_live(conn, sid, 1)
    _lock(conn, sid, "AAA")
    now = dt.datetime.now(ET)
    progress.store_kickoffs(conn, sid, 1, now - dt.timedelta(days=1), now + dt.timedelta(hours=1))
    assert not progress.statuses(conn, sid, 1, now=now)[a]["done"]
    assert progress.statuses(conn, sid, 1, now=now + dt.timedelta(hours=2))[a]["done"]


def test_a_decided_matchup_counts_before_the_week_is_over():
    conn, sid, (a, b, c, d) = _mini_season(("Aces", "Bees", "Cats", "Dogs"))
    for h, aw in ((a, b), (c, d)):
        conn.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id,away_team_id) "
                     "VALUES (?,1,'CONFERENCE',?,?)", (sid, h, aw))
    for tid, nfl, each in ((a, "AAA", 3), (b, "BBB", 5), (c, "CCC", 4), (d, "DDD", 6)):
        _starters(conn, sid, tid, nfl, each)
    scoring.score_team_week(conn, sid, 1)
    _in_progress(conn, sid)
    _lock(conn, sid, "AAA")
    _lock(conn, sid, "BBB")                    # Aces v Bees is decided; Cats v Dogs isn't
    rec = {t["team_id"]: (t["wins"], t["losses"]) for t in st.compute_standings(conn, sid)["BLUE"]}
    assert rec[b] == (1, 0) and rec[a] == (0, 1)
    assert rec[c] == (0, 0) and rec[d] == (0, 0)


def test_elimination_is_called_once_it_is_certain():
    conn, sid, (a, b, c) = _mini_season()
    _starters(conn, sid, a, "AAA", 3)          # 27
    _starters(conn, sid, b, "BBB", 5)          # 45
    _starters(conn, sid, c, "CCC", 4)          # 36
    _in_progress(conn, sid)
    _lock(conn, sid, "AAA")
    _lock(conn, sid, "BBB")
    assert st.try_early_elimination(conn, sid, 1) == []      # Cats have 0 locked in: could be lowest
    _lock(conn, sid, "CCC")
    assert st.try_early_elimination(conn, sid, 1) == [a]
    assert conn.execute("SELECT alive, eliminated_ff_week FROM teams WHERE id=?",
                        (a,)).fetchone()[:] == (0, 1)


def test_a_team_still_playing_is_safe_once_it_has_locked_in_more():
    conn, sid, (a, b, c) = _mini_season()
    _starters(conn, sid, a, "AAA", 3)                         # 27, done
    _starters(conn, sid, b, "BBB", 5, later="MON", n_later=2)  # 35 locked, 2 play Monday
    _starters(conn, sid, c, "CCC", 4)                         # 36, done
    _in_progress(conn, sid)
    for nfl in ("AAA", "BBB", "CCC"):
        _lock(conn, sid, nfl)
    assert st.try_early_elimination(conn, sid, 1) == [a]


def test_a_possible_tie_waits_then_eliminates_everyone_tied():
    conn, sid, (a, b, c) = _mini_season()
    _starters(conn, sid, a, "AAA", 7)                         # 63, done
    _starters(conn, sid, b, "BBB", 9, later="MON", n_later=2)  # 63 locked, 2 still to play
    _starters(conn, sid, c, "CCC", 8)                         # 72, done
    _in_progress(conn, sid)
    for nfl in ("AAA", "BBB", "CCC"):
        _lock(conn, sid, nfl)
    assert st.try_early_elimination(conn, sid, 1) == []      # Bees could still tie at 63
    _lock(conn, sid, "MON")                                   # ... and they score nothing
    assert sorted(st.try_early_elimination(conn, sid, 1)) == sorted([a, b])


def test_the_final_run_does_not_eliminate_twice():
    conn, sid, (a, b, c) = _mini_season()
    for tid, nfl, each in ((a, "AAA", 3), (b, "BBB", 5), (c, "CCC", 4)):
        _starters(conn, sid, tid, nfl, each)
        _lock(conn, sid, nfl)
    scoring.score_team_week(conn, sid, 1)
    _in_progress(conn, sid)
    assert st.try_early_elimination(conn, sid, 1) == [a]
    assert st.run_elimination(conn, sid, 1) == [a]
    assert conn.execute("SELECT COUNT(*) c FROM teams WHERE alive=0").fetchone()["c"] == 1


# --- carrying a lineup forward -------------------------------------------------

# NFL teams -> bye FF week. T4+T5 on bye wk5 (two receivers), T6 wk9 (one).
NFL = [("KC", None), ("BAL", None), ("PIT", None), ("CIN", None),
       ("T1", 8), ("T2", 8), ("T3", None), ("T4", 5), ("T5", 5), ("T6", 9), ("T7", None),
       ("T8", None)]
PLAYERS = [("r1", "RB", "T1"), ("r2", "RB", "T2"), ("r3", "RB", "T3"),
           ("w1", "WR", "T4"), ("w2", "WR", "T5"), ("w3", "WR", "T6"), ("w4", "TE", "T7"),
           ("fa_r", "WR", "T8"), ("fa_rb", "RB", "T8")]          # free agents
ROSTER = [("TEAM_UNIT", "KC", "C"), ("TEAM_UNIT", "BAL", "K"), ("TEAM_UNIT", "PIT", "DEF/ST"),
          ("TEAM_UNIT", "CIN", "QB"), ("PLAYER", "r1", "RB"), ("PLAYER", "r2", "RB"),
          ("PLAYER", "r3", "RB"), ("PLAYER", "w1", "R"), ("PLAYER", "w2", "R"),
          ("PLAYER", "w3", "R"), ("PLAYER", "w4", "R")]


@pytest.fixture()
def lg():
    c = schema.connect(":memory:")
    schema.init_db(c)
    schema.migrate(c)
    sid = schema.seed_reference(c)
    c.execute("UPDATE seasons SET ff_start_nfl_week=3 WHERE id=?", (sid,))
    for abbr, bye in NFL:
        c.execute("INSERT INTO nfl_teams(season_id,abbr,name,bye_ff_week) VALUES (?,?,?,?)",
                  (sid, abbr, abbr, bye))
    for gid, pos, team in PLAYERS:
        c.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr) "
                  "VALUES (?,?,?,?,?)", (sid, gid, gid, pos, team))
    otb = c.execute("SELECT id FROM teams WHERE name='OT Blitz'").fetchone()["id"]
    for kind, ref, slot in ROSTER:
        c.execute("INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,unit_type,"
                  "roster_slot,acquired_ff_week,acquired_via,created_at) "
                  "VALUES (?,?,?,?,?,?,1,'DRAFT','t')",
                  (sid, otb, kind, ref, slot if kind == "TEAM_UNIT" else None, slot))
    c.commit()
    return c, sid, otb


def _set(conn, sid, otb, wk, rbs, rs):
    units = [{"roster_slot": s, "asset_ref": r}
             for s, r in (("C", "KC"), ("K", "BAL"), ("DEF/ST", "PIT"), ("QB", "CIN"))]
    repo.set_lineup(conn, sid, otb, wk, units
                    + [{"roster_slot": "RB", "asset_ref": r} for r in rbs]
                    + [{"roster_slot": "R", "asset_ref": r} for r in rs])


def _got(conn, sid, otb, wk):
    rows = conn.execute("SELECT roster_slot, asset_ref, is_rental, carried_from, carry_note "
                        "FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week=?",
                        (sid, otb, wk)).fetchall()
    return ({r["asset_ref"] for r in rows if r["roster_slot"] == "RB"},
            {r["asset_ref"] for r in rows if r["roster_slot"] == "R"}, rows)


def test_a_plain_copy_when_nothing_has_changed(lg):
    conn, sid, otb = lg
    _set(conn, sid, otb, 4, ["r1", "r2"], ["w1", "w2", "w3"])
    assert carry.carry_team(conn, sid, otb, 6)
    rb, r, rows = _got(conn, sid, otb, 6)
    assert (rb, r) == ({"r1", "r2"}, {"w1", "w2", "w3"})
    assert len(rows) == 9 and all(x["carried_from"] == 4 and not x["carry_note"] for x in rows)
    assert not carry.carry_team(conn, sid, otb, 6)          # never twice


def test_one_receiver_on_bye_is_replaced_from_the_bench(lg):
    conn, sid, otb = lg
    _set(conn, sid, otb, 4, ["r1", "r2"], ["w1", "w2", "w3"])
    carry.carry_team(conn, sid, otb, 9)                     # w3 on bye
    assert _got(conn, sid, otb, 9)[:2] == ({"r1", "r2"}, {"w1", "w2", "w4"})


def test_two_receivers_on_bye_bring_in_the_extra_rb(lg):
    conn, sid, otb = lg
    _set(conn, sid, otb, 4, ["r1", "r2"], ["w1", "w2", "w3"])
    carry.carry_team(conn, sid, otb, 5)                     # w1 and w2 on bye
    rb, r, rows = _got(conn, sid, otb, 5)
    assert (rb, r) == ({"r1", "r2", "r3"}, {"w3", "w4"})
    assert not rows[0]["carry_note"]


def test_after_the_bye_week_the_lineup_goes_back_to_before_it(lg):
    """Case A: the extra RB sits, the receivers who were on bye return."""
    conn, sid, otb = lg
    _set(conn, sid, otb, 4, ["r1", "r2"], ["w1", "w2", "w3"])
    _set(conn, sid, otb, 5, ["r1", "r2", "r3"], ["w3", "w4"])   # the flex, w1+w2 on bye
    carry.carry_team(conn, sid, otb, 6)
    assert _got(conn, sid, otb, 6)[:2] == ({"r1", "r2"}, {"w1", "w2", "w3"})


def test_a_flex_with_nothing_to_go_back_to_is_left_for_the_commissioner(lg):
    conn, sid, otb = lg
    _set(conn, sid, otb, 5, ["r1", "r2", "r3"], ["w3", "w4"])
    carry.carry_team(conn, sid, otb, 6)
    rb, _, rows = _got(conn, sid, otb, 6)
    assert rb == {"r1", "r2", "r3"}                         # copied as-is, not guessed at
    assert "no earlier lineup" in rows[0]["carry_note"]


def test_a_traded_player_is_replaced_by_the_one_who_came_in(lg):
    conn, sid, otb = lg
    _set(conn, sid, otb, 4, ["r1", "r2"], ["w1", "w2", "w3"])
    repo.do_trade(conn, sid, otb, "R", "w1", "fa_r", 5)
    carry.carry_team(conn, sid, otb, 6)
    assert _got(conn, sid, otb, 6)[1] == {"fa_r", "w2", "w3"}


def test_last_weeks_open_gives_way_to_the_player_it_covered(lg):
    conn, sid, otb = lg
    repo.do_open(conn, sid, otb, "R", "w1", "fa_r", 5)      # w1 on bye wk5
    _set(conn, sid, otb, 5, ["r1", "r2"], ["fa_r", "w3", "w4"])
    carry.carry_team(conn, sid, otb, 6)
    rb, r, rows = _got(conn, sid, otb, 6)
    assert r == {"w1", "w3", "w4"} and not any(x["is_rental"] for x in rows)


def test_this_weeks_open_starts_in_a_carried_lineup(lg):
    """And a receiver covered by an Open doesn't count toward the flex: with
    w1 covered only w2 is an uncovered bye, so it's the bench, not the extra RB."""
    conn, sid, otb = lg
    _set(conn, sid, otb, 4, ["r1", "r2"], ["w1", "w2", "w3"])
    repo.do_open(conn, sid, otb, "R", "w1", "fa_r", 5)
    carry.carry_team(conn, sid, otb, 5)
    rb, r, rows = _got(conn, sid, otb, 5)
    assert (rb, r) == ({"r1", "r2"}, {"fa_r", "w3", "w4"})
    assert [x["is_rental"] for x in rows if x["asset_ref"] == "fa_r"] == [1]


def test_a_covered_open_does_not_count_toward_the_flex(lg):
    conn, sid, otb = lg
    repo.do_open(conn, sid, otb, "R", "w1", "fa_r", 5)
    with pytest.raises(repo.RuleError):
        _set(conn, sid, otb, 5, ["r1", "r2", "r3"], ["fa_r", "w3"])   # only w2 uncovered


def test_a_carried_player_whose_game_has_started_is_locked_in(lg):
    """P is locked in: the copy is the lineup that was in effect at his kickoff."""
    conn, sid, otb = lg
    _set(conn, sid, otb, 4, ["r1", "r2"], ["w1", "w2", "w3"])
    carry.carry_team(conn, sid, otb, 6)
    units = [{"roster_slot": s, "asset_ref": r}
             for s, r in (("C", "KC"), ("K", "BAL"), ("DEF/ST", "PIT"), ("QB", "CIN"))]
    rbs = [{"roster_slot": "RB", "asset_ref": r} for r in ("r1", "r2")]
    with pytest.raises(repo.RuleError):                     # can't bench w3 once he's played
        repo.set_lineup(conn, sid, otb, 6, units + rbs + [
            {"roster_slot": "R", "asset_ref": r} for r in ("w1", "w2", "w4")],
            locked_refs={"w3"})
    repo.set_lineup(conn, sid, otb, 6, units + rbs + [      # anyone else can still change
        {"roster_slot": "R", "asset_ref": r} for r in ("w4", "w2", "w3")], locked_refs={"w3"})


def test_lineups_carry_only_for_the_week_being_played(lg, monkeypatch):
    """At the first kickoff, not before — and never into a week long over, so a
    finished season's empty weeks are never filled in after the fact."""
    conn, sid, otb = lg
    _set(conn, sid, otb, 4, ["r1", "r2"], ["w1", "w2", "w3"])
    conn.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id) VALUES (?,6,'BYE',?)",
                 (sid, otb))
    conn.commit()
    kickoff = dt.datetime(2026, 10, 25, 13, 0, tzinfo=ET)        # FF wk 6 = NFL wk 8
    games = pd.DataFrame([{"season": 2026, "week": 8, "game_id": "G", "home_team": "T7",
                           "away_team": "T8", "home_score": float("nan"),
                           "gameday": "2026-10-25", "gametime": "13:00"}])
    monkeypatch.setattr(nv, "load_games", lambda: games)
    monkeypatch.setattr(nv, "finished_games", lambda season: set())

    def week6():
        return conn.execute("SELECT COUNT(*) c FROM weekly_lineups WHERE team_id=? "
                            "AND ff_week=6", (otb,)).fetchone()["c"]

    runner.run_current(conn, sid, now=kickoff - dt.timedelta(hours=1))
    assert week6() == 0
    runner.run_current(conn, sid, now=kickoff + dt.timedelta(days=30))
    assert week6() == 0
    runner.run_current(conn, sid, now=kickoff + dt.timedelta(minutes=5))
    assert week6() == 9


# --- where each starter's game stands (box-score tags) -------------------------

def test_every_unfinished_starter_is_tagged_and_zero_is_not_ambiguous():
    """TallBears at 0 points with 6 to play: the box score has to say which six."""
    conn, sid, (a, _, _) = _mini_season()
    _starters(conn, sid, a, "FIN", 0, later="LIV", n_later=5)      # 4 finished with 0 pts
    conn.execute("UPDATE nfl_players SET nfl_team_abbr='UPC' WHERE gsis_id=?", (f"p{a}_6",))
    conn.execute("UPDATE nfl_players SET nfl_team_abbr='OVR' WHERE gsis_id=?", (f"p{a}_7",))
    conn.execute("UPDATE nfl_players SET nfl_team_abbr='BYE' WHERE gsis_id=?", (f"p{a}_8",))
    conn.execute("INSERT INTO nfl_teams(season_id,abbr,name,bye_ff_week) VALUES (?,'BYE','Bye',1)",
                 (sid,))
    now = dt.datetime(2026, 9, 13, 16, 0, tzinfo=ET)
    progress.store_week_games(conn, sid, 1, [
        ("G1", "FIN", "X1", now - dt.timedelta(hours=4), True),
        ("G2", "LIV", "X2", now - dt.timedelta(minutes=30), False),
        ("G3", "UPC", "X3", now + dt.timedelta(hours=3), False),
        ("G4", "OVR", "X4", now - dt.timedelta(hours=3), True)])
    progress.mark_live(conn, sid, 1)
    _lock(conn, sid, "FIN")
    states = progress.starter_states(conn, sid, 1, a, now=now)
    by = {}
    for (_, ref), s in states.items():
        by.setdefault(s["state"], []).append(ref)
    assert len(by["final"]) == 4                 # untagged, even though they scored 0
    assert len(by["playing"]) == 2 and by["upcoming"] == [f"p{a}_6"]
    assert by["over"] == [f"p{a}_7"] and by["bye"] == [f"p{a}_8"]
    upc = states[("R", f"p{a}_6")]
    assert upc["kickoff"] == (now + dt.timedelta(hours=3)).isoformat()


def test_a_settled_week_tags_nobody():
    conn, sid, (a, _, _) = _mini_season()
    _starters(conn, sid, a, "AAA", 3)
    scoring.score_team_week(conn, sid, 1)
    progress.mark_finalized(conn, sid, 1)
    assert {s["state"] for s in progress.starter_states(conn, sid, 1, a).values()} == {"final"}


def test_the_hourly_run_records_each_weeks_games(lg, monkeypatch):
    conn, sid, otb = lg
    conn.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id) VALUES (?,6,'BYE',?)",
                 (sid, otb))
    conn.commit()
    games = pd.DataFrame([
        {"season": 2026, "week": 8, "game_id": "G_A", "home_team": "T7", "away_team": "T8",
         "home_score": 20.0, "gameday": "2026-10-25", "gametime": "13:00"},
        {"season": 2026, "week": 8, "game_id": "G_B", "home_team": "T1", "away_team": "T2",
         "home_score": float("nan"), "gameday": "2026-10-26", "gametime": "20:15"}])
    monkeypatch.setattr(nv, "load_games", lambda: games)
    monkeypatch.setattr(nv, "finished_games", lambda season: set())
    runner.run_current(conn, sid, now=dt.datetime(2026, 10, 1, tzinfo=ET))
    got = {r["game_id"]: (r["kickoff"], r["final"]) for r in conn.execute(
        "SELECT game_id, kickoff, final FROM nfl_week_games WHERE ff_week=6")}
    assert got == {"G_A": ("2026-10-25T13:00:00-04:00", 1),
                   "G_B": ("2026-10-26T20:15:00-04:00", 0)}


def test_an_in_progress_card_shows_final_points_only(tmp_path):
    """A mid-game stats update can hold half a game. The card must not show it:
    the Bees' 4 starters still playing have points stored, but only their 5
    finished starters count toward what the card displays."""
    import json

    from joyce_ff.webapp import create_app

    path = str(tmp_path / "league.sqlite")
    conn, sid, (a, b, _) = _mini_season(path=path)
    conn.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id,away_team_id) "
                 "VALUES (?,1,'CONFERENCE',?,?)", (sid, a, b))
    _starters(conn, sid, a, "AAA", 3)                              # 27, all final
    _starters(conn, sid, b, "BBB", 5, later="LATE", n_later=4)      # 25 final + 4 playing
    conn.execute("UPDATE asset_week_scores SET points=4 WHERE asset_ref IN "
                 "(SELECT gsis_id FROM nfl_players WHERE nfl_team_abbr='LATE')")   # partial file
    scoring.score_team_week(conn, sid, 1)
    _in_progress(conn, sid)
    _lock(conn, sid, "AAA")
    _lock(conn, sid, "BBB")
    conn.close()

    c = create_app(path).test_client()
    card = json.loads(c.get("/api/state?week=1").data)["scoreboard"][0]
    assert card["home"]["points"] == 27
    assert card["away"]["points"] == 25                 # not the stored 41
    assert not card["away"]["done"]
