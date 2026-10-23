"""Conference play, Weeks 5-15 (commissioner, 2026-10-22): the two conference
byes play each other, and each week's top score in each conference wins $10."""

from joyce_ff.league import progress, schema, standings
from test_league_season import _assign_team_numbers, db  # noqa: F401  (fixture)


def _team(conn, sid, conf, number):
    return conn.execute("SELECT t.id FROM teams t JOIN conferences c ON c.id=t.conference_id "
                        "WHERE t.season_id=? AND c.code=? AND t.team_number=?",
                        (sid, conf, number)).fetchone()["id"]


def _scores(conn, sid, wk, points: dict):
    for tid, p in points.items():
        conn.execute("INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points) VALUES (?,?,?,?)",
                     (sid, tid, wk, p))
    conn.commit()


# --- the crossover ---------------------------------------------------------------

def test_week_5_byes_play_each_other(db):  # noqa: F811
    conn, sid = db
    _assign_team_numbers(conn, sid)
    standings.generate_matchups(conn, sid)
    (game,) = conn.execute("SELECT kind, home_team_id h, away_team_id a FROM matchups WHERE season_id=? "
                           "AND ff_week=5 AND kind='INTERLEAGUE'", (sid,)).fetchall()
    assert (game["h"], game["a"]) == (_team(conn, sid, "BLUE", 11), _team(conn, sid, "RED", 11))
    for wk in range(5, 16):                                    # one crossover a week, every week
        assert conn.execute("SELECT COUNT(*) n FROM matchups WHERE season_id=? AND ff_week=? "
                            "AND kind='INTERLEAGUE'", (sid, wk)).fetchone()["n"] == 1


def test_it_counts_in_the_overall_record_not_the_conference_one(db):  # noqa: F811
    conn, sid = db
    _assign_team_numbers(conn, sid)
    standings.generate_matchups(conn, sid)
    blue, red = _team(conn, sid, "BLUE", 11), _team(conn, sid, "RED", 11)
    _scores(conn, sid, 5, {blue: 30, red: 20})
    s = {t["team_id"]: t for conf in ("BLUE", "RED") for t in standings.compute_standings(conn, sid)[conf]}
    assert (s[blue]["wins"], s[blue]["conf_wins"]) == (1, 0)
    assert (s[red]["losses"], s[red]["conf_losses"]) == (1, 0)


def test_a_season_built_with_byes_gets_its_crossovers_on_reload(db):  # noqa: F811
    """The live schedule was built before this: each week had a Blue BYE row
    and a Red one. migrate() pairs them up, for the newest season only."""
    conn, sid = db
    _assign_team_numbers(conn, sid)
    standings.generate_matchups(conn, sid)
    blue, red = _team(conn, sid, "BLUE", 11), _team(conn, sid, "RED", 11)
    conn.execute("DELETE FROM matchups WHERE season_id=? AND ff_week=5 AND kind='INTERLEAGUE'", (sid,))
    for tid in (blue, red):                                    # as it was stored before
        conn.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id) VALUES (?,5,'BYE',?)", (sid, tid))
    conn.execute("UPDATE seasons SET year=year+1 WHERE id=?", (sid,))    # keep it the newest
    old = schema.seed_reference(conn, year=2020, label="2020-21")        # an archived season with byes
    pair = conn.execute("SELECT t.id FROM teams t JOIN conferences c ON c.id=t.conference_id "
                        "WHERE t.season_id=? ORDER BY c.code, t.id", (old,)).fetchall()
    for tid in (pair[0]["id"], pair[-1]["id"]):
        conn.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id) VALUES (?,5,'BYE',?)", (old, tid))
    conn.commit()
    schema.migrate(conn)
    schema.migrate(conn)                                       # twice: nothing more happens
    rows = conn.execute("SELECT kind, home_team_id h, away_team_id a FROM matchups WHERE season_id=? "
                        "AND ff_week=5 AND kind IN ('INTERLEAGUE','BYE')", (sid,)).fetchall()
    assert [(r["kind"], r["h"], r["a"]) for r in rows] == [("INTERLEAGUE", blue, red)]
    assert conn.execute("SELECT COUNT(*) n FROM matchups WHERE season_id=? AND kind='BYE'",
                        (old,)).fetchone()["n"] == 2                   # the old season is left alone


# --- the weekly high score -----------------------------------------------------------

def test_the_top_score_in_each_conference_wins_10_and_a_tie_splits_it(db):  # noqa: F811
    conn, sid = db
    _assign_team_numbers(conn, sid)
    standings.generate_matchups(conn, sid)
    b1, b2, b11 = (_team(conn, sid, "BLUE", n) for n in (1, 2, 11))
    r1, r2, r3 = (_team(conn, sid, "RED", n) for n in (1, 2, 3))
    _scores(conn, sid, 4, {b1: 99, r1: 99})                    # Week 4: interleague, no prize
    _scores(conn, sid, 5, {b1: 30, b2: 25, b11: 41,            # the crossover team counts for Blue
                           r1: 33, r2: 33, r3: 10})
    progress.mark_finalized(conn, sid, 4)
    assert standings.weekly_high_scores(conn, sid) == []       # Week 5 isn't final yet
    progress.mark_finalized(conn, sid, 5)
    (wk5,) = standings.weekly_high_scores(conn, sid)
    assert wk5["week"] == 5
    assert [(w["team_id"], w["cents"]) for w in wk5["BLUE"]["winners"]] == [(b11, 1000)]
    assert sorted((w["team_id"], w["cents"]) for w in wk5["RED"]["winners"]) == [(r1, 500), (r2, 500)]
    highs = standings.weekly_high_scores(conn, sid)
    assert standings.team_high_scores(highs, r1) == [{"week": 5, "cents": 500, "tie": True}]
    # a stat correction that changes the leader moves the $10 with it
    conn.execute("UPDATE team_week_scores SET computed_points=34 WHERE team_id=? AND ff_week=5", (r2,))
    conn.commit()
    (wk5,) = standings.weekly_high_scores(conn, sid)
    assert [(w["team_id"], w["cents"]) for w in wk5["RED"]["winners"]] == [(r2, 1000)]


def test_a_three_way_tie_pays_whole_cents(db):  # noqa: F811
    conn, sid = db
    _assign_team_numbers(conn, sid)
    ids = [_team(conn, sid, "BLUE", n) for n in (1, 2, 3)]
    _scores(conn, sid, 6, {i: 20 for i in ids})
    progress.mark_finalized(conn, sid, 6)
    (wk6,) = standings.weekly_high_scores(conn, sid)
    assert [w["cents"] for w in wk6["BLUE"]["winners"]] == [333, 333, 333]
