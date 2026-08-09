"""
Season setup: load the NFL universe (players + team units with bye weeks) from
nflverse, assign team numbers / draft slots, and materialize the schedule.
"""

from __future__ import annotations


def _nfl_byes(g) -> dict[str, int | None]:
    """NFL bye week per team = the regular-season week (1-18) it doesn't play."""
    reg = g[g["game_type"] == "REG"]
    teams = sorted(set(reg["home_team"]) | set(reg["away_team"]))
    byes = {}
    for abbr in teams:
        played = set(reg[(reg["home_team"] == abbr) | (reg["away_team"] == abbr)]["week"])
        missing = [w for w in range(1, 19) if w not in played]
        byes[abbr] = missing[0] if missing else None
    return byes


def load_nfl_universe(conn, season_id: int, year: int) -> tuple[int, int]:
    """Populate nfl_teams (with FF bye week) and nfl_players (RB/WR/TE)."""
    from ..data_sources import nflverse as nv

    ff_start = conn.execute("SELECT ff_start_nfl_week FROM seasons WHERE id=?",
                            (season_id,)).fetchone()["ff_start_nfl_week"]
    games = nv.load_games()
    g = games[games["season"] == year]
    byes = _nfl_byes(g)
    for abbr, nfl_bye in byes.items():
        bye_ff = (nfl_bye - (ff_start - 1)) if nfl_bye else None
        conn.execute("INSERT OR IGNORE INTO nfl_teams(season_id,abbr,name,bye_ff_week) "
                     "VALUES (?,?,?,?)", (season_id, abbr, abbr, bye_ff))

    roster = nv.load_roster(year)
    ros = roster[roster["position"].isin(["RB", "WR", "TE"])]
    n = 0
    for _, r in ros.iterrows():
        if not r.get("gsis_id"):
            continue
        conn.execute("INSERT OR IGNORE INTO nfl_players(season_id,gsis_id,name,position,"
                     "nfl_team_abbr,status) VALUES (?,?,?,?,?,?)",
                     (season_id, r["gsis_id"], r["full_name"], r["position"],
                      r.get("team"), r.get("status")))
        n += 1
    conn.commit()
    return len(byes), n


def assign_numbers_and_slots(conn, season_id: int) -> None:
    """Give each team a team_number (schedule) and draft_slot (card), 1-11 per
    conference. Deterministic — real draws replace these on draft day."""
    for conf in ("BLUE", "RED"):
        rows = conn.execute(
            "SELECT t.id FROM teams t JOIN conferences c ON c.id=t.conference_id "
            "WHERE t.season_id=? AND c.code=? ORDER BY t.id", (season_id, conf)).fetchall()
        for i, r in enumerate(rows, 1):
            conn.execute("UPDATE teams SET team_number=?, draft_slot=? WHERE id=?",
                         (i, i, r["id"]))
    conn.commit()


def prepare_season(conn, season_id: int, year: int) -> None:
    from . import standings as st
    load_nfl_universe(conn, season_id, year)
    assign_numbers_and_slots(conn, season_id)
    st.generate_matchups(conn, season_id)
