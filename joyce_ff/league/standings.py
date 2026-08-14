"""
Matchups, standings (with the real tiebreaker chain), and the elimination pool.

All pure DB logic — no network — so it's fully testable.

Playoff seeding: top 8 per conference by overall record; tiebreak
head-to-head -> conference record -> conference points -> coin flip (we use a
deterministic name order as the stand-in for the coin flip).
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import groupby

from ..schedule import rotation


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- schedule materialization -------------------------------------------

def generate_matchups(conn, season_id: int) -> int:
    """Build the season's matchups from the Team# rotation. Requires every team
    to have team_number set. Regenerates (clears existing). Returns row count."""
    teams = conn.execute(
        "SELECT t.id, t.team_number, c.code conf FROM teams t "
        "JOIN conferences c ON c.id=t.conference_id WHERE t.season_id=?", (season_id,)).fetchall()
    if any(t["team_number"] is None for t in teams):
        raise ValueError("all teams need a team_number before generating matchups")
    by_key = {(t["conf"], t["team_number"]): t["id"] for t in teams}

    conn.execute("DELETE FROM matchups WHERE season_id=?", (season_id,))
    n = 0
    for t in teams:
        for wk in range(1, rotation.NO_PLAY_WEEK):     # 1..15
            g = rotation.opponent(t["team_number"], t["conf"], wk)
            if g["kind"] == "bye":
                conn.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id) "
                             "VALUES (?,?, 'BYE', ?)", (season_id, wk, t["id"]))
                n += 1
                continue
            opp_id = by_key[(g["opp_conf"], g["opp_team"])]
            if t["id"] < opp_id:                       # dedup unordered pair
                kind = "INTERLEAGUE" if g["kind"] == "interleague" else "CONFERENCE"
                conn.execute("INSERT INTO matchups(season_id,ff_week,kind,home_team_id,away_team_id) "
                             "VALUES (?,?,?,?,?)", (season_id, wk, kind, t["id"], opp_id))
                n += 1
    conn.commit()
    return n


# --- records & standings -------------------------------------------------

def _blank(team) -> dict:
    return {"team_id": team["id"], "name": team["name"], "conf": team["conf"],
            "wins": 0, "losses": 0, "ties": 0, "conf_wins": 0, "conf_losses": 0,
            "pf": 0.0, "pa": 0.0}


def compute_standings(conn, season_id: int, through_week: int | None = None) -> dict:
    """Return {'BLUE': [ranked team dicts], 'RED': [...]} with records, PF/PA,
    and playoff seed (1-based). Only games where both teams have a score count.
    """
    teams = conn.execute(
        "SELECT t.id, t.name, c.code conf FROM teams t "
        "JOIN conferences c ON c.id=t.conference_id WHERE t.season_id=?", (season_id,)).fetchall()
    stats = {t["id"]: _blank(t) for t in teams}

    scores = {}   # (team_id, week) -> points
    for r in conn.execute("SELECT team_id, ff_week, computed_points FROM team_week_scores "
                          "WHERE season_id=? AND computed_points IS NOT NULL", (season_id,)):
        scores[(r["team_id"], r["ff_week"])] = r["computed_points"]

    results = []  # (winner_id, loser_id) for H2H, ties excluded
    q = "SELECT ff_week, kind, home_team_id, away_team_id FROM matchups WHERE season_id=? AND away_team_id IS NOT NULL"
    for m in conn.execute(q, (season_id,)):
        if through_week is not None and m["ff_week"] > through_week:
            continue
        h, a = m["home_team_id"], m["away_team_id"]
        hs, as_ = scores.get((h, m["ff_week"])), scores.get((a, m["ff_week"]))
        if hs is None or as_ is None:
            continue
        stats[h]["pf"] += hs; stats[h]["pa"] += as_
        stats[a]["pf"] += as_; stats[a]["pa"] += hs
        conf_game = m["kind"] == "CONFERENCE"
        if hs == as_:
            stats[h]["ties"] += 1; stats[a]["ties"] += 1
        else:
            w, l = (h, a) if hs > as_ else (a, h)
            stats[w]["wins"] += 1; stats[l]["losses"] += 1
            if conf_game:
                stats[w]["conf_wins"] += 1; stats[l]["conf_losses"] += 1
            results.append((w, l))

    return {conf: _rank([s for s in stats.values() if s["conf"] == conf], results)
            for conf in ("BLUE", "RED")}


def _rank(teamstats: list[dict], results: list[tuple]) -> list[dict]:
    """Sort a conference by wins, breaking ties H2H -> conf record -> PF ->
    name (coin-flip stand-in). Assigns 1-based seed."""
    teamstats.sort(key=lambda t: -t["wins"])
    ranked = []
    for _wins, grp in groupby(teamstats, key=lambda t: t["wins"]):
        grp = list(grp)
        if len(grp) > 1:
            ids = {t["team_id"] for t in grp}
            h2h = {tid: 0 for tid in ids}
            for w, l in results:
                if w in ids and l in ids:
                    h2h[w] += 1
            grp.sort(key=lambda t: (-h2h[t["team_id"]], -t["conf_wins"], -t["pf"], t["name"]))
        ranked.extend(grp)
    for seed, t in enumerate(ranked, 1):
        t["seed"] = seed
        t["playoffs"] = seed <= 8
    return ranked


# --- elimination pool ----------------------------------------------------

def run_elimination(conn, season_id: int, ff_week: int) -> list[int]:
    """Eliminate the lowest-scoring team(s) AMONG SURVIVORS this week. Per the
    commissioner: a tie for the lowest score eliminates ALL tied teams (no
    tiebreak). Returns the list of eliminated team_ids (empty if none scored).

    Idempotent: if elimination already happened this week, return those ids."""
    already = [r["id"] for r in conn.execute(
        "SELECT id FROM teams WHERE season_id=? AND eliminated_ff_week=?",
        (season_id, ff_week))]
    if already:
        return already
    rows = conn.execute(
        "SELECT tw.team_id, tw.computed_points FROM team_week_scores tw "
        "JOIN teams t ON t.id=tw.team_id "
        "WHERE tw.season_id=? AND tw.ff_week=? AND t.alive=1 AND tw.computed_points IS NOT NULL",
        (season_id, ff_week)).fetchall()
    if not rows:
        return []
    low = min(r["computed_points"] for r in rows)
    tied = [r["team_id"] for r in rows if r["computed_points"] == low]
    conn.executemany("UPDATE teams SET alive=0, eliminated_ff_week=? WHERE id=?",
                     [(ff_week, tid) for tid in tied])
    conn.commit()
    return tied


FINAL_WEEK = 15                 # 15-game season; the pool pays out after wk 15
TOP_PRIZE_CENTS = 10000         # $100 to the top score (split if tied)
SURVIVOR_PRIZE_CENTS = 1000     # $10 to every other surviving team


def final_payout(conn, season_id: int, final_week: int = FINAL_WEEK) -> dict | None:
    """Elimination-pool payout after the final week. The lowest scorer(s) are
    already eliminated for that week (run_elimination); among the survivors the
    top score splits $100 and every other survivor gets $10. Returns None until
    the final week is scored."""
    rows = conn.execute(
        "SELECT t.id, t.name, t.alive, tw.computed_points pts "
        "FROM teams t JOIN team_week_scores tw ON tw.season_id=t.season_id "
        "AND tw.team_id=t.id AND tw.ff_week=? "
        "WHERE t.season_id=? AND (t.alive=1 OR t.eliminated_ff_week=?) "
        "AND tw.computed_points IS NOT NULL",
        (final_week, season_id, final_week)).fetchall()
    if not rows:
        return None
    top = max(r["pts"] for r in rows)
    survivors = [r for r in rows if r["alive"]]          # remaining after wk-15 cut
    winners = [r for r in survivors if r["pts"] == top]
    others = [r for r in survivors if r["pts"] != top]
    per_winner = TOP_PRIZE_CENTS // len(winners) if winners else 0
    return {"final_week": final_week, "top_points": top,
            "winners": [{"name": r["name"], "points": r["pts"], "cents": per_winner}
                        for r in winners],
            "others": [{"name": r["name"], "points": r["pts"], "cents": SURVIVOR_PRIZE_CENTS}
                       for r in sorted(others, key=lambda r: (-r["pts"], r["name"]))]}


def pool_status(conn, season_id: int) -> dict:
    alive, dead = [], []
    for r in conn.execute("SELECT name, alive, eliminated_ff_week FROM teams WHERE season_id=? "
                          "ORDER BY eliminated_ff_week, name", (season_id,)):
        (alive if r["alive"] else dead).append(
            {"name": r["name"], "eliminated_ff_week": r["eliminated_ff_week"]})
    return {"alive": alive, "eliminated": dead}
