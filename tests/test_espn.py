"""ESPN as the fast scoring source, nflverse as backup and cross-check.

What these pin down:
  * the reader turns an ESPN box score into exactly the stats we score, with
    ESPN's team spellings mapped onto ours (WSH -> WAS, LAR -> LA);
  * a game locks from ESPN only after showing Final for a few minutes, and only
    when every scoring play is understood and every player with stats matched —
    otherwise it's left for nflverse rather than locked on a guess;
  * one source failing doesn't stop scoring; all failing says why;
  * when nflverse later scores a locked starter differently, it's noted for the
    commissioner and nothing changes;
  * nflverse's reader now counts lateral yards, as official stats do.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from joyce_ff.data_sources import espn
from joyce_ff.data_sources import nflverse as nv
from joyce_ff.league import progress, schema, scoring

ET = progress.ET


def _athletes(rows):
    return [{"athlete": {"id": aid, "displayName": name}, "stats": stats} for aid, name, stats in rows]


def _summary(scoring_plays=None, rb_espn_id="101"):
    """A BUF @ HOU box score: HOU's RB runs for 88 and a TD; BUF gets a pick-six."""
    def block(ab, rushing=(), passing=(), kicking=(), defensive=(), ints=()):
        return {"team": {"abbreviation": ab}, "statistics": [
            {"name": "passing", "labels": ["C/ATT", "YDS", "AVG", "TD", "INT"], "athletes": _athletes(passing)},
            {"name": "rushing", "labels": ["CAR", "YDS", "AVG", "TD", "LONG"], "athletes": _athletes(rushing)},
            {"name": "receiving", "labels": ["REC", "YDS", "AVG", "TD", "LONG", "TGTS"], "athletes": []},
            {"name": "kicking", "labels": ["FG", "PCT", "LONG", "XP", "PTS"], "athletes": _athletes(kicking)},
            {"name": "defensive", "labels": ["TOT", "SOLO", "SACKS", "TFL", "PD", "QB HTS", "TD"],
             "athletes": _athletes(defensive)},
            {"name": "interceptions", "labels": ["INT", "YDS", "TD"], "athletes": _athletes(ints)}]}

    return {
        "boxscore": {
            "players": [
                block("HOU", rushing=[(rb_espn_id, "Runner Back", ["20", "88", "4.4", "1", "30"])],
                      passing=[("201", "Hou QB", ["20/30", "250", "8", "1", "1"])],
                      kicking=[("301", "Hou K", ["1/1", "100", "44", "2/2", "5"])]),
                block("BUF", passing=[("202", "Buf QB", ["22/31", "301", "9", "2", "0"])],
                      defensive=[("401", "Buf DE", ["5", "4", "1.5", "1", "0", "2", "0"]),
                                 ("402", "Buf LB", ["7", "5", "1.5", "0", "1", "1", "0"])],
                      ints=[("403", "Buf CB", ["1", "40", "1"])]),
            ],
            "teams": [
                {"team": {"abbreviation": "HOU"}, "statistics": [
                    {"name": "totalYards", "displayValue": "381"}, {"name": "fumblesLost", "displayValue": "1"}]},
                {"team": {"abbreviation": "BUF"}, "statistics": [
                    {"name": "totalYards", "displayValue": "409"}, {"name": "fumblesLost", "displayValue": "0"}]},
            ]},
        "header": {"competitions": [{"competitors": [
            {"homeAway": "home", "team": {"abbreviation": "HOU"}, "score": "17"},
            {"homeAway": "away", "team": {"abbreviation": "BUF"}, "score": "24"}]}]},
        "scoringPlays": scoring_plays if scoring_plays is not None else [
            {"team": {"abbreviation": "HOU"}, "type": {"text": "Field Goal Good"}, "text": "Hou K 44 Yd Field Goal"},
            {"team": {"abbreviation": "HOU"}, "type": {"text": "Rushing Touchdown"}, "text": "Runner Back 5 Yd Rush"},
            {"team": {"abbreviation": "BUF"}, "type": {"text": "Interception Return Touchdown"}, "text": "pick six"},
        ],
    }


def _scoreboard(state="post", completed=True):
    return {"events": [{"id": "900", "status": {"type": {"state": state, "completed": completed}},
                        "competitions": [{"competitors": [
                            {"homeAway": "home", "team": {"abbreviation": "HOU"}},
                            {"homeAway": "away", "team": {"abbreviation": "BUF"}}]}]}]}


def _feed(monkeypatch, summary=None, board=None):
    summary = summary or _summary()
    board = board or _scoreboard()
    monkeypatch.setattr(espn, "_get", lambda url, timeout=30: board if "scoreboard" in url else summary)


# --- the reader -------------------------------------------------------------

def test_the_reader_reduces_a_box_score_to_what_we_score(monkeypatch):
    _feed(monkeypatch)
    g = espn.game_lines("900")
    rb = g.players["101"]
    assert (rb["team"], rb["rushing_yards"], rb["rushing_tds"]) == ("HOU", 88, 1)
    hou, buf = g.units["HOU"], g.units["BUF"]
    assert hou["passing_yards"] == 250 and hou["fg_distances"] == [44] and hou["extra_points_made"] == 2
    assert buf["sacks"] == 3 and buf["interceptions"] == 1 and buf["defensive_tds"] == 1
    assert buf["points_allowed"] == 17 and buf["yards_allowed"] == 381 and buf["fumble_recoveries"] == 1
    assert buf["won"] and not hou["won"]
    assert g.unknown_scoring == []


def test_espn_team_spellings_map_onto_ours(monkeypatch):
    board = {"events": [{"id": "1", "status": {"type": {"state": "pre", "completed": False}},
                         "competitions": [{"competitors": [
                             {"homeAway": "home", "team": {"abbreviation": "LAR"}},
                             {"homeAway": "away", "team": {"abbreviation": "WSH"}}]}]}]}
    monkeypatch.setattr(espn, "_get", lambda url, timeout=30: board)
    ev = espn.week_events(2026, 3)[0]
    assert (ev.home, ev.away, ev.game_id) == ("LA", "WAS", "2026_03_WAS_LA")


def test_a_scoring_play_it_cannot_classify_is_flagged(monkeypatch):
    plays = [{"team": {"abbreviation": "BUF"}, "type": {"text": "Penalty"}, "text": "?"}]
    _feed(monkeypatch, summary=_summary(scoring_plays=plays))
    assert espn.game_lines("900").unknown_scoring == ["Penalty"]


def test_a_changed_feed_fails_loudly_rather_than_half_parsing(monkeypatch):
    monkeypatch.setattr(espn, "_get", lambda url, timeout=30: {"boxscore": {}})
    with pytest.raises(espn.Unavailable):
        espn.game_lines("900")


# --- locking from ESPN ---------------------------------------------------------

@pytest.fixture()
def season(monkeypatch):
    conn = schema.connect(":memory:")
    schema.init_db(conn)
    schema.migrate(conn)
    conn.execute("INSERT INTO seasons(year,label,current_ff_week,ff_start_nfl_week) VALUES (2026,'2026-27',1,1)")
    sid = conn.execute("SELECT id FROM seasons").fetchone()["id"]
    conn.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr) "
                 "VALUES (?,'g_rb','Runner Back','RB','HOU')", (sid,))
    conn.commit()
    games = pd.DataFrame([{"season": 2026, "week": 1, "game_id": "2026_01_BUF_HOU",
                           "home_team": "HOU", "away_team": "BUF", "home_score": 17.0}])
    monkeypatch.setattr(nv, "load_games", lambda: games)
    monkeypatch.setattr(scoring, "_espn_to_gsis", lambda year: {"101": "g_rb"})
    monkeypatch.setattr(scoring, "_roster_by_name", lambda year: {})
    return conn, sid


def test_a_player_without_an_espn_id_is_matched_by_name_and_team(season, monkeypatch):
    """nflverse's roster lacks ESPN ids for some players (a rookie RB in 2026)."""
    conn, sid = season
    monkeypatch.setattr(scoring, "_espn_to_gsis", lambda year: {})
    monkeypatch.setattr(scoring, "_gsis_by_name", lambda *a: None)
    monkeypatch.setattr(scoring, "_roster_by_name", lambda year: {("runnerback", "HOU"): "g_rb"})
    _feed(monkeypatch)
    t0 = dt.datetime(2026, 9, 13, 16, 0, tzinfo=ET)
    scoring.ingest_espn_week(conn, sid, 1, now=t0)
    scoring.ingest_espn_week(conn, sid, 1, now=t0 + dt.timedelta(minutes=11))
    assert _locks(conn) == [("2026_01_BUF_HOU", "espn")]


def _locks(conn):
    return [(r["game_id"], r["source"]) for r in conn.execute("SELECT game_id, source FROM nfl_game_locks")]


def test_a_final_game_locks_once_it_has_been_final_a_few_minutes(season, monkeypatch):
    conn, sid = season
    _feed(monkeypatch)
    t0 = dt.datetime(2026, 9, 13, 16, 0, tzinfo=ET)
    scoring.ingest_espn_week(conn, sid, 1, now=t0)
    assert _locks(conn) == []                                   # first sighting of Final
    rb = conn.execute("SELECT points FROM asset_week_scores WHERE asset_ref='g_rb'").fetchone()
    assert rb["points"] > 0                                     # scored all the same
    scoring.ingest_espn_week(conn, sid, 1, now=t0 + dt.timedelta(minutes=11))
    assert _locks(conn) == [("2026_01_BUF_HOU", "espn")]


def test_a_game_in_progress_never_locks(season, monkeypatch):
    conn, sid = season
    _feed(monkeypatch, board=_scoreboard(state="in", completed=False))
    t0 = dt.datetime(2026, 9, 13, 15, 0, tzinfo=ET)
    for minutes in (0, 30, 60):
        scoring.ingest_espn_week(conn, sid, 1, now=t0 + dt.timedelta(minutes=minutes))
    assert _locks(conn) == []


@pytest.mark.parametrize("summary", [
    _summary(rb_espn_id="999"),                                   # a scorer we can't match
    _summary(scoring_plays=[{"team": {"abbreviation": "BUF"}, "type": {"text": "Penalty"}, "text": "?"}]),
])
def test_a_game_it_does_not_fully_understand_is_left_for_nflverse(season, monkeypatch, summary):
    conn, sid = season
    monkeypatch.setattr(scoring, "_gsis_by_name", lambda *a: None)
    _feed(monkeypatch, summary=summary)
    t0 = dt.datetime(2026, 9, 13, 16, 0, tzinfo=ET)
    scoring.ingest_espn_week(conn, sid, 1, now=t0)
    scoring.ingest_espn_week(conn, sid, 1, now=t0 + dt.timedelta(hours=1))
    assert _locks(conn) == []


def test_a_locked_game_is_not_rewritten(season, monkeypatch):
    conn, sid = season
    _feed(monkeypatch)
    t0 = dt.datetime(2026, 9, 13, 16, 0, tzinfo=ET)
    scoring.ingest_espn_week(conn, sid, 1, now=t0)
    scoring.ingest_espn_week(conn, sid, 1, now=t0 + dt.timedelta(minutes=11))
    before = conn.execute("SELECT points FROM asset_week_scores WHERE asset_ref='g_rb'").fetchone()["points"]
    changed = _summary()
    changed["boxscore"]["players"][0]["statistics"][1]["athletes"][0]["stats"][1] = "30"
    _feed(monkeypatch, summary=changed)
    scoring.ingest_espn_week(conn, sid, 1, now=t0 + dt.timedelta(hours=2))
    assert conn.execute("SELECT points FROM asset_week_scores WHERE asset_ref='g_rb'").fetchone()["points"] == before


# --- sources together ------------------------------------------------------------

def test_one_source_down_is_fine_all_down_says_why(season, monkeypatch):
    conn, sid = season
    boom = espn.Unavailable("ESPN request failed")
    monkeypatch.setattr(scoring, "ingest_espn_week", lambda *a, **k: (_ for _ in ()).throw(boom))
    monkeypatch.setattr(scoring, "ingest_asset_scores_from_nflverse", lambda *a, **k: 7)
    assert scoring.ingest_week(conn, sid, 1) == {"nflverse": 7}

    nope = nv.NotPublishedYet("nflverse hasn't published 2026 play-by-play yet.")
    monkeypatch.setattr(scoring, "ingest_asset_scores_from_nflverse", lambda *a, **k: (_ for _ in ()).throw(nope))
    with pytest.raises(nv.NotPublishedYet):
        scoring.ingest_week(conn, sid, 1)


def test_a_later_difference_is_noted_and_nothing_changes(season):
    conn, sid = season
    tid = conn.execute("INSERT INTO conferences(code,name) VALUES ('BLUE','Blue')").lastrowid
    conn.execute("INSERT INTO teams(season_id,conference_id,name,team_number) VALUES (?,?,'Aces',1)", (sid, tid))
    team = conn.execute("SELECT id FROM teams").fetchone()["id"]
    conn.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,asset_ref) "
                 "VALUES (?,?,1,'RB','PLAYER','g_rb')", (sid, team))
    conn.execute("INSERT INTO asset_week_scores(season_id,ff_week,asset_kind,asset_ref,unit_type,points,"
                 "computed_at) VALUES (?,1,'PLAYER','g_rb','',8,'t')", (sid,))
    scoring._check_locked(conn, sid, 1, "PLAYER", "g_rb", None, 8, "nflverse")      # same: nothing
    scoring._check_locked(conn, sid, 1, "PLAYER", "nobody", None, 6, "nflverse")    # not started
    assert conn.execute("SELECT COUNT(*) c FROM stat_checks").fetchone()["c"] == 0
    scoring._check_locked(conn, sid, 1, "PLAYER", "g_rb", None, 10, "nflverse")
    row = conn.execute("SELECT locked_points, other_points FROM stat_checks").fetchone()
    assert (row["locked_points"], row["other_points"]) == (8, 10)
    assert conn.execute("SELECT points FROM asset_week_scores WHERE asset_ref='g_rb'").fetchone()["points"] == 8


# --- the always-on checker's gate -------------------------------------------------

def test_the_checker_only_wakes_while_a_game_is_on_and_unlocked(season):
    conn, sid = season
    ko = dt.datetime(2026, 9, 13, 12, 0, tzinfo=ET)
    progress.store_week_games(conn, sid, 1, [("2026_01_BUF_HOU", "HOU", "BUF", ko, False)])
    conn.commit()
    assert not progress.games_to_watch(conn, sid, now=ko - dt.timedelta(minutes=5))
    assert progress.games_to_watch(conn, sid, now=ko + dt.timedelta(hours=2))
    conn.execute("INSERT INTO nfl_game_locks(season_id,ff_week,game_id,home_team,away_team,locked_at,source) "
                 "VALUES (?,1,'2026_01_BUF_HOU','HOU','BUF','t','espn')", (sid,))
    assert not progress.games_to_watch(conn, sid, now=ko + dt.timedelta(hours=3))


# --- nflverse: lateral yards ---------------------------------------------------------

def test_lateral_yards_count_as_official_stats_do():
    """Allen to Coleman for 1, lateral to Shakir for 10; then Allen to Shakir for 19."""
    base = {"week": 1, "posteam": "BUF", "rusher_player_id": None, "rusher_player_name": None,
            "rushing_yards": None, "rush_touchdown": 0, "passer_player_id": "qb", "passer_player_name": "J.Allen",
            "pass_touchdown": 0, "return_touchdown": 0, "td_player_id": None, "td_team": None,
            "td_player_name": None, "lateral_rusher_player_id": None, "lateral_rusher_player_name": None,
            "lateral_rushing_yards": None, "complete_pass": 1}
    pbp = pd.DataFrame([
        {**base, "receiver_player_id": "coleman", "receiver_player_name": "K.Coleman", "receiving_yards": 1,
         "passing_yards": 11, "lateral_receiver_player_id": "shakir",
         "lateral_receiver_player_name": "K.Shakir", "lateral_receiving_yards": 10},
        {**base, "receiver_player_id": "shakir", "receiver_player_name": "K.Shakir", "receiving_yards": 19,
         "passing_yards": 19, "lateral_receiver_player_id": None, "lateral_receiver_player_name": None,
         "lateral_receiving_yards": None},
    ])
    out = nv.player_week_stats(pbp).set_index("player_id")
    assert out.loc["shakir", "receiving_yards"] == 29 and out.loc["shakir", "receptions"] == 1
    assert out.loc["coleman", "receiving_yards"] == 1
