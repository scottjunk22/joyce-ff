"""
Breaking a tied game (commissioner, 2026-09-14).

A matchup tied on points goes to the team whose DEF/ST allowed the fewest NET
yards — the defense each team actually started that week, a rented one (an
Open) included. A DEF/ST on bye that wasn't covered by an Open loses. If both
were on bye, or the yards are equal (when Blue plays Red both teams can start
the same NFL defense), the commissioner decides; there is no further
tiebreaker. The same rule applies to any matchup: regular season, playoffs,
Super Bowl.

The elimination pool is unaffected: a tie for the week's lowest score still
eliminates everyone tied.
"""

from __future__ import annotations


def _team_name(conn, team_id) -> str:
    r = conn.execute("SELECT name FROM teams WHERE id=?", (team_id,)).fetchone()
    return r["name"] if r else str(team_id)


def _defense(conn, season_id, ff_week, team_id):
    """(NFL team, net yards allowed) for the DEF/ST a team started, or None if
    it had no defense that played (none started, or on bye). Yards is None
    while not yet recorded."""
    d = conn.execute(
        "SELECT l.asset_ref, t.bye_ff_week FROM weekly_lineups l "
        "LEFT JOIN nfl_teams t ON t.season_id=l.season_id AND t.abbr=l.asset_ref "
        "WHERE l.season_id=? AND l.ff_week=? AND l.team_id=? AND l.roster_slot='DEF/ST'",
        (season_id, ff_week, team_id)).fetchone()
    if d is None or d["bye_ff_week"] == ff_week:
        return None
    y = conn.execute(
        "SELECT yards_allowed FROM asset_week_scores WHERE season_id=? AND ff_week=? "
        "AND asset_kind='TEAM_UNIT' AND asset_ref=? AND unit_type='DEF/ST'",
        (season_id, ff_week, d["asset_ref"])).fetchone()
    return d["asset_ref"], (y["yards_allowed"] if y else None)


def decide(conn, season_id: int, ff_week: int, home_id: int, away_id: int) -> dict:
    """How a tied matchup is settled.

    {"winner": team_id or None, "text": what the site says,
     "needs_commissioner": True when only he can settle it,
     "decided": True when he has}"""
    names = {home_id: _team_name(conn, home_id), away_id: _team_name(conn, away_id)}
    ruled = conn.execute(
        "SELECT winner_team_id FROM tiebreak_decisions WHERE season_id=? AND ff_week=? "
        "AND home_team_id=? AND away_team_id=?", (season_id, ff_week, home_id, away_id)).fetchone()
    if ruled:
        w = ruled["winner_team_id"]
        return {"winner": w, "text": f"{names[w]} wins tie — commissioner's decision",
                "needs_commissioner": False, "decided": True}

    h, a = _defense(conn, season_id, ff_week, home_id), _defense(conn, season_id, ff_week, away_id)

    def settled(w, why):
        return {"winner": w, "text": f"{names[w]} wins tiebreaker: {why}",
                "needs_commissioner": False, "decided": False}

    def commissioner(why):
        return {"winner": None, "text": f"Tie: {why} — commissioner to decide",
                "needs_commissioner": True, "decided": False}

    if h is None and a is None:
        return commissioner("both DEF/ST on bye")
    if h is None:
        return settled(away_id, f"{names[home_id]}'s DEF/ST was on bye")
    if a is None:
        return settled(home_id, f"{names[away_id]}'s DEF/ST was on bye")
    if h[1] is None or a[1] is None:
        return {"winner": None, "text": "Tie: waiting for yards allowed",
                "needs_commissioner": False, "decided": False}
    if h[1] == a[1]:
        return commissioner(f"equal yards allowed ({h[1]})")
    w, (wy, ly) = (home_id, (h[1], a[1])) if h[1] < a[1] else (away_id, (a[1], h[1]))
    return settled(w, f"{wy} yards allowed vs {ly}")
