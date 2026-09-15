"""Lineup labels on the game cards (commissioner, 2026-09-15).

Most managers set their lineup Sunday morning, so a card only says something
when it's worth acting on: "⚠ Thu player" before Thursday's kickoff for a team
that hasn't submitted and has a player whose game is before Sunday; grey "last
week's lineup" Sunday 8am-noon for a copied lineup; nothing from Sunday noon.
"""

from __future__ import annotations

import datetime as dt

import pytest

from joyce_ff.league import progress, schema

CT = progress.CT


def at(day, hour, minute=0):
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=CT)


@pytest.fixture()
def week2():
    c = schema.connect(":memory:")
    schema.init_db(c)
    schema.migrate(c)
    c.execute("INSERT INTO seasons(year,label,current_ff_week,ff_start_nfl_week) VALUES (2026,'2026-27',2,1)")
    sid = c.execute("SELECT id FROM seasons").fetchone()["id"]
    cid = c.execute("INSERT INTO conferences(code,name) VALUES ('BLUE','Blue')").lastrowid
    ids = {}
    for n in ("Thursday", "Sunday", "Submitted"):
        ids[n] = c.execute("INSERT INTO teams(season_id,conference_id,name,team_number) VALUES (?,?,?,?)",
                           (sid, cid, n, len(ids) + 1)).lastrowid
    # Thursday 7:15pm game LA v SF; Sunday noon games otherwise.
    progress.store_kickoffs(c, sid, 2, at(17, 19, 15), at(21, 19, 15))
    progress.store_week_games(c, sid, 2, [("G1", "LA", "SF", at(17, 19, 15), False),
                                          ("G2", "KC", "BUF", at(20, 12, 0), False)])
    for gid, name, team in (("p_kyren", "Kyren Williams", "LA"), ("p_puka", "Puka Nacua", "LA"),
                            ("p_kelce", "Travis Kelce", "KC")):
        c.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr) VALUES (?,?,?,?,?)",
                  (sid, gid, name, "WR", team))
    for team, ref in (("Thursday", "p_kyren"), ("Thursday", "p_puka"), ("Sunday", "p_kelce"),
                      ("Submitted", "p_puka")):
        c.execute("INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,roster_slot,"
                  "acquired_ff_week,acquired_via,created_at) VALUES (?,?,'PLAYER',?,'R',1,'DRAFT','t')",
                  (sid, ids[team], ref))
    c.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,asset_ref) "
              "VALUES (?,?,2,'R','PLAYER','p_puka')", (sid, ids["Submitted"]))
    c.commit()

    def flags(now):
        f = progress.lineup_flags(c, sid, 2, now)
        return {name: (f[i]["kind"], f[i]["text"]) if i in f else None for name, i in ids.items()}

    def copy_lineups():
        for team in ("Thursday", "Sunday"):
            c.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,asset_ref,"
                      "carried_from) VALUES (?,?,2,'R','PLAYER','p_kelce',1)", (sid, ids[team]))
        c.commit()

    return c, sid, flags, copy_lineups


def test_before_thursday_only_a_team_with_a_thursday_player_is_warned(week2):
    _, _, flags, _ = week2
    assert flags(at(16, 12, 0)) == {"Thursday": ("early", "Thu players"), "Sunday": None, "Submitted": None}


def test_the_warning_names_the_players_and_their_kickoff(week2):
    c, sid, _, _ = week2
    title = progress.lineup_flags(c, sid, 2, at(16, 12, 0))
    (thursday,) = [v for v in title.values()]
    assert thursday["title"].startswith("Kyren Williams (Thu 7:15 PM), Puka Nacua (Thu 7:15 PM)")


def test_after_the_copy_the_cards_are_quiet_until_sunday_morning(week2):
    _, _, flags, copy_lineups = week2
    copy_lineups()
    for when in (at(17, 19, 30), at(18, 20, 0), at(20, 7, 59)):
        assert flags(when) == {"Thursday": None, "Sunday": None, "Submitted": None}


def test_sunday_morning_shows_who_is_still_on_last_weeks_lineup(week2):
    _, _, flags, copy_lineups = week2
    copy_lineups()
    expected = ("carried", "last week's lineup")
    assert flags(at(20, 8, 0)) == {"Thursday": expected, "Sunday": expected, "Submitted": None}
    assert flags(at(20, 11, 59))["Sunday"] == expected


def test_from_sunday_noon_the_cards_carry_no_lineup_labels(week2):
    _, _, flags, copy_lineups = week2
    copy_lineups()
    assert flags(at(20, 12, 0)) == {"Thursday": None, "Sunday": None, "Submitted": None}


def test_a_team_with_nothing_to_copy_is_warned_sunday_morning(week2):
    _, _, flags, _ = week2
    assert flags(at(20, 9, 0))["Sunday"] == ("none", "no lineup")


# --- the Thursday banner, the count, and copied lineups named as such ------------

def test_the_banner_lists_teams_with_no_lineup_and_a_player_in_the_game(week2):
    c, sid, _, _ = week2
    alerts = progress.early_lineup_alerts(c, sid, 2, at(16, 12, 0))
    assert [(a["game"], a["when"], [t["name"] for t in a["teams"]]) for a in alerts] == \
        [("SF @ LAR", "Thu 7:15 PM", ["Thursday"])]


def test_the_banner_is_gone_once_the_game_kicks_off_or_the_team_submits(week2):
    c, sid, _, copy_lineups = week2
    assert progress.early_lineup_alerts(c, sid, 2, at(17, 19, 16)) == []
    copy_lineups()
    assert progress.early_lineup_alerts(c, sid, 2, at(16, 12, 0)) == []


def test_to_play_counts_wait_for_sunday_noon(week2):
    c, sid, _, _ = week2
    assert not progress.counts_visible(c, sid, 2, at(18, 20, 0))
    assert not progress.counts_visible(c, sid, 2, at(20, 11, 59))
    assert progress.counts_visible(c, sid, 2, at(20, 12, 0))
