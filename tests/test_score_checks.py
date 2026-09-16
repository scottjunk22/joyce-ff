"""The commissioner's score double-check list and stat check buttons (2026-09-15)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from joyce_ff.league import progress, schema, score_checks

CT = progress.CT


def at(month, day, hour=12):
    return dt.datetime(2026, month, day, hour, tzinfo=CT)


@pytest.fixture()
def lg():
    c = schema.connect(":memory:")
    schema.init_db(c)
    schema.migrate(c)
    c.execute("INSERT INTO seasons(year,label,current_ff_week,ff_start_nfl_week) VALUES (2026,'2026-27',1,1)")
    sid = c.execute("SELECT id FROM seasons").fetchone()["id"]
    c.commit()
    return c, sid


def week(c, sid, wk, sunday, games, locks=(), finalized=True):
    """games: [(game_id, home, away)] all at Sunday noon; locks: {game_id: (source, verified)}."""
    ko = at(9, sunday)
    progress.store_kickoffs(c, sid, wk, ko, ko)
    progress.store_week_games(c, sid, wk, [(g, h, a, ko, True) for g, h, a in games])
    for gid, (source, verified) in dict(locks).items():
        h, a = next((h, a) for g, h, a in games if g == gid)
        c.execute("INSERT INTO nfl_game_locks(season_id,ff_week,game_id,home_team,away_team,locked_at,"
                  "source,verified_at) VALUES (?,?,?,?,?,?,?,?)",
                  (sid, wk, gid, h, a, "2026-09-01T00:00:00+00:00", source,
                   "2026-09-02T14:00:00+00:00" if verified else None))
    if finalized:
        progress.mark_finalized(c, sid, wk)
    c.commit()


GAMES = [("G1", "KC", "DEN"), ("G2", "BUF", "MIA"), ("G3", "LA", "SF")]


def test_a_week_counts_checked_games_and_says_what_the_rest_are_waiting_on(lg):
    c, sid = lg
    week(c, sid, 1, 13, GAMES, {"G1": ("espn", True), "G2": ("espn", False), "G3": ("nflverse", False)})
    (w,) = score_checks.weeks(c, sid, now=at(9, 14))["recent"]
    assert (w["checked"], w["total"], w["finished"]) == (2, 3, False)
    assert [(g["game"], g["status"]) for g in w["games"]] == [
        ("MIA @ BUF", "waiting"), ("DEN @ KC", "checked"), ("SF @ LAR", "checked")]


def test_a_game_never_posted_stays_listed_until_dismissed(lg):
    c, sid = lg
    week(c, sid, 1, 13, GAMES, {"G1": ("espn", True), "G2": ("espn", False), "G3": ("espn", True)})
    (w,) = score_checks.weeks(c, sid, now=at(9, 20))["recent"]       # window closed
    assert (w["unchecked"], w["can_dismiss"], w["finished"]) == (["MIA @ BUF"], True, False)
    score_checks.dismiss_week(c, sid, 1, "Steve")
    (w,) = score_checks.weeks(c, sid, now=at(9, 20))["recent"]
    assert (w["dismissed"], w["can_dismiss"], w["finished"]) == (True, False, True)


def test_three_weeks_show_unfinished_ones_always_and_finished_ones_newest_first(lg):
    c, sid = lg
    done = {g: ("espn", True) for g, _, _ in GAMES}
    week(c, sid, 2, 6, GAMES, {**done, "G2": ("espn", False)})        # never posted: unfinished
    week(c, sid, 3, 13, GAMES, done)
    week(c, sid, 4, 20, GAMES, done)
    week(c, sid, 5, 27, GAMES, {"G1": ("espn", True)}, finalized=False)
    lists = score_checks.weeks(c, sid, now=at(9, 28))
    assert [w["week"] for w in lists["recent"]] == [5, 4, 2]
    assert [w["week"] for w in lists["earlier"]] == [3]


def test_a_week_not_started_is_not_listed(lg):
    c, sid = lg
    week(c, sid, 1, 13, GAMES, finalized=False)
    assert score_checks.weeks(c, sid, now=at(9, 12))["recent"] == []


def _stat_check(c, sid):
    t = c.execute("INSERT INTO teams(season_id,conference_id,name,team_number) VALUES (?,1,'OT Blitz',1)",
                  (sid,)).lastrowid
    c.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,asset_ref) "
              "VALUES (?,?,1,'R','PLAYER','p_jj')", (sid, t))
    c.execute("INSERT INTO asset_week_scores(season_id,ff_week,asset_kind,asset_ref,unit_type,points,"
              "breakdown_json,computed_at) VALUES (?,1,'PLAYER','p_jj','',18,'[[\"TD\",6]]','t')", (sid,))
    cid = c.execute("INSERT INTO stat_checks(season_id,ff_week,asset_kind,asset_ref,unit_type,locked_points,"
                    "other_points,other_breakdown,source,noted_at) VALUES (?,1,'PLAYER','p_jj','',18,20,"
                    "'[[\"2 receptions\",2],[\"1 receiving TD\",6],[\"1 2-pt conversion\",2],[\"120 rec yds\",10]]',"
                    "'nflverse','t')", (sid,)).lastrowid
    c.commit()
    return t, cid


def test_changing_a_stat_check_rewrites_the_line_and_the_team_total(lg):
    c, sid = lg
    c.execute("INSERT INTO conferences(id,code,name) VALUES (1,'BLUE','Blue')")
    team, cid = _stat_check(c, sid)
    score_checks.resolve_stat_check(c, sid, cid, "change", "Steve")
    line = c.execute("SELECT points, breakdown_json FROM asset_week_scores").fetchone()
    # nflverse's own lines explain the new total, with a note of what it was.
    assert line["points"] == 20
    assert json.loads(line["breakdown_json"]) == [
        ["2 receptions", 2], ["1 receiving TD", 6], ["1 2-pt conversion", 2], ["120 rec yds", 10],
        ["corrected from nflverse · was 18", 0]]
    assert c.execute("SELECT computed_points FROM team_week_scores WHERE team_id=?",
                     (team,)).fetchone()["computed_points"] == 20
    assert c.execute("SELECT resolution FROM stat_checks").fetchone()["resolution"] == "changed"
    # A later cross-check refreshes the lines but keeps the "corrected" note.
    from joyce_ff.scoring.models import ScoreBreakdown
    b = ScoreBreakdown()
    b.add("1 receiving TD", 6)
    b.add("140 rec yds", 14)
    from joyce_ff.league import scoring
    scoring._check_locked(c, sid, 1, "PLAYER", "p_jj", None, b, "nflverse")
    assert json.loads(c.execute("SELECT breakdown_json FROM asset_week_scores").fetchone()["breakdown_json"]) == [
        ["1 receiving TD", 6], ["140 rec yds", 14], ["corrected from nflverse · was 18", 0]]
    with pytest.raises(ValueError):
        score_checks.resolve_stat_check(c, sid, cid, "keep", "Steve")


def test_keeping_a_stat_check_changes_nothing_but_settles_it(lg):
    c, sid = lg
    c.execute("INSERT INTO conferences(id,code,name) VALUES (1,'BLUE','Blue')")
    _, cid = _stat_check(c, sid)
    score_checks.resolve_stat_check(c, sid, cid, "keep", "Steve")
    assert c.execute("SELECT points FROM asset_week_scores").fetchone()["points"] == 18
    assert c.execute("SELECT resolution FROM stat_checks").fetchone()["resolution"] == "kept"
