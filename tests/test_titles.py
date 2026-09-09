"""Matching 36 years of hand-typed champion names against this season's teams.

The bias under test is one-directional: a team that never won must never be
crowned, even when the names are close. Missing a crown is recoverable.
"""

from __future__ import annotations

from joyce_ff.league import schema, titles


def _db(tmp_path, champs, teams):
    conn = schema.connect(str(tmp_path / "t.sqlite"))
    schema.init_db(conn)
    schema.migrate(conn)
    conn.execute("INSERT INTO seasons(year,label,current_ff_week) VALUES (2026,'2026-27',1)")
    sid = conn.execute("SELECT id FROM seasons").fetchone()["id"]
    for year, label, team in champs:
        conn.execute("INSERT INTO champions(year,label,team) VALUES (?,?,?)", (year, label, team))
    conn.execute("INSERT OR IGNORE INTO conferences(code,name) VALUES ('BLUE','Blue')")
    cid = conn.execute("SELECT id FROM conferences WHERE code='BLUE'").fetchone()["id"]
    for i, name in enumerate(teams, start=1):
        conn.execute("INSERT INTO teams(season_id,conference_id,name,team_number) "
                     "VALUES (?,?,?,?)", (sid, cid, name, i))
    conn.commit()
    return conn, sid


def test_spacing_and_manager_parentheticals_still_match(tmp_path):
    conn, sid = _db(tmp_path,
                    [(2025, "2025-26", "Shuffling Crew")],
                    ["ShufflingCrew (Joe)"])
    got = titles.for_season(conn, sid)
    assert len(got) == 1
    entry = next(iter(got.values()))
    assert entry["count"] == 1 and entry["defending"] is True


def test_a_similar_name_is_not_crowned(tmp_path):
    """'BarnBrners' vs 'Barn Burners' is a typo, not a match — and we don't
    guess. The commissioner fixes the record or adds an alias."""
    conn, sid = _db(tmp_path, [(2021, "2021-22", "BarnBrners")], ["Barn Burners"])
    assert titles.for_season(conn, sid) == {}


def test_an_alias_the_commissioner_reports_does_match(tmp_path, monkeypatch):
    monkeypatch.setattr(titles, "ALIASES", {"BarnBrners": "Barn Burners"})
    monkeypatch.setattr(titles, "_ALIAS_N", None)
    conn, sid = _db(tmp_path, [(2021, "2021-22", "BarnBrners")], ["Barn Burners"])
    assert titles.for_season(conn, sid)


def test_repeat_winner_lists_every_year_newest_first(tmp_path):
    conn, sid = _db(tmp_path,
                    [(1993, "1993-94", "Bad Boys"), (1994, "1994-95", "Bad Boys"),
                     (2013, "2013-14", "Bad Boys"), (2025, "2025-26", "Ribears")],
                    ["Bad Boys", "Ribears"])
    by_name = {conn.execute("SELECT name FROM teams WHERE id=?", (k,)).fetchone()["name"]: v
               for k, v in titles.for_season(conn, sid).items()}
    assert by_name["Bad Boys"]["years"] == ["2013-14", "1994-95", "1993-94"]
    assert by_name["Bad Boys"]["defending"] is False      # only one crown flies
    assert by_name["Ribears"]["defending"] is True


def test_teams_that_never_won_are_absent(tmp_path):
    conn, sid = _db(tmp_path, [(2025, "2025-26", "Ribears")], ["Ribears", "OT Blitz"])
    assert len(titles.for_season(conn, sid)) == 1


def test_the_crown_belongs_to_the_season_being_viewed(tmp_path):
    """Looking back at an old season must not crown today's champion. The
    defending team in season Y is the winner of Y-1, whoever that was."""
    conn, _ = _db(tmp_path,
                  [(2024, "2024-25", "Bad Boys"), (2025, "2025-26", "Ribears")],
                  ["Bad Boys", "Ribears"])
    conn.execute("INSERT INTO seasons(year,label,current_ff_week) VALUES (2025,'2025-26',1)")
    old = conn.execute("SELECT id FROM seasons WHERE year=2025").fetchone()["id"]
    new = conn.execute("SELECT id FROM seasons WHERE year=2026").fetchone()["id"]
    cid = conn.execute("SELECT id FROM conferences WHERE code='BLUE'").fetchone()["id"]
    for i, name in enumerate(["Bad Boys", "Ribears"], start=1):   # same two teams, both seasons
        conn.execute("INSERT INTO teams(season_id,conference_id,name,team_number) "
                     "VALUES (?,?,?,?)", (old, cid, name, i))
    conn.commit()

    def crowned(sid):
        return {conn.execute("SELECT name FROM teams WHERE id=?", (k,)).fetchone()["name"]
                for k, v in titles.for_season(conn, sid).items() if v["defending"]}

    assert crowned(new) == {"Ribears"}      # 2026-27: Ribears defend
    assert crowned(old) == {"Bad Boys"}     # 2025-26: Bad Boys were defending
