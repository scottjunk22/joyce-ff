"""
The weekly automation runner — the "it scores itself" command.

run_week() orchestrates the pipeline for one FF week: pull the NFL stats, score
every team from its submitted lineup, run the elimination step, and advance the
season. Idempotent — safe to re-run when a stat corrects.

reconcile_week() compares our computed totals against the legacy site's posted
totals (populated by scrape.py) and flags any disagreement.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import scoring
from . import standings as st


def carry_forward_lineups(conn, season_id: int, ff_week: int) -> int:
    """Rule: a team that didn't set a lineup keeps last week's. Copies the most
    recent prior week's (non-rental) starters for any team missing one."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    filled = 0
    for r in conn.execute("SELECT id FROM teams WHERE season_id=?", (season_id,)):
        tid = r["id"]
        if conn.execute("SELECT 1 FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week=? LIMIT 1",
                        (season_id, tid, ff_week)).fetchone():
            continue
        prev = conn.execute(
            "SELECT MAX(ff_week) w FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week<?",
            (season_id, tid, ff_week)).fetchone()["w"]
        if prev is None:
            continue
        for s in conn.execute(
            "SELECT roster_slot, asset_kind, asset_ref, unit_type FROM weekly_lineups "
            "WHERE season_id=? AND team_id=? AND ff_week=? AND is_rental=0",
                (season_id, tid, prev)):
            conn.execute(
                "INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,"
                "asset_ref,unit_type,is_rental,submitted_at) VALUES (?,?,?,?,?,?,?,0,?)",
                (season_id, tid, ff_week, s["roster_slot"], s["asset_kind"], s["asset_ref"],
                 s["unit_type"], now))
        filled += 1
    conn.commit()
    return filled


def run_week(conn, season_id: int, ff_week: int, *, do_ingest: bool = True,
             eliminate: bool = True) -> dict:
    summary: dict = {"ff_week": ff_week}
    if do_ingest:
        summary["assets_scored"] = scoring.ingest_asset_scores_from_nflverse(conn, season_id, ff_week)
    summary["lineups_carried"] = carry_forward_lineups(conn, season_id, ff_week)
    scoring.score_team_week(conn, season_id, ff_week)
    if eliminate:
        summary["eliminated_team_id"] = st.run_elimination(conn, season_id, ff_week)
    conn.execute("UPDATE seasons SET current_ff_week=MAX(current_ff_week, ?) WHERE id=?",
                 (ff_week, season_id))
    conn.commit()
    summary["team_scores"] = {r["team_id"]: r["computed_points"] for r in conn.execute(
        "SELECT team_id, computed_points FROM team_week_scores WHERE season_id=? AND ff_week=?",
        (season_id, ff_week))}
    return summary


def run_current(conn, season_id: int) -> list[int]:
    """Score every FF week that (a) has lineups submitted and (b) whose NFL week
    now has final scores. Idempotent — the host runs this on a daily schedule
    and it picks up each week automatically once the games finish. Skips the
    stat pull for weeks already ingested."""
    from ..data_sources import nflverse as nv

    year = conn.execute("SELECT year FROM seasons WHERE id=?", (season_id,)).fetchone()["year"]
    g = nv.load_games()
    g = g[g["season"] == year]
    done = {int(w) for w, grp in g.groupby("week") if grp["home_score"].notna().all()}
    weeks = [r["ff_week"] for r in conn.execute(
        "SELECT DISTINCT ff_week FROM weekly_lineups WHERE season_id=? ORDER BY ff_week",
        (season_id,))]
    ran = []
    for ff in weeks:
        if scoring.nfl_week_for(conn, season_id, ff) in done:
            have = conn.execute("SELECT 1 FROM asset_week_scores WHERE season_id=? AND ff_week=? LIMIT 1",
                                (season_id, ff)).fetchone()
            run_week(conn, season_id, ff, do_ingest=not have)
            ran.append(ff)
    return ran


def reconcile_week(conn, season_id: int, ff_week: int, tol: float = 0.5) -> dict:
    """Compare our computed team totals to the legacy site's posted totals.
    Returns matched count + any mismatches (|computed - posted| > tol)."""
    rows = conn.execute(
        "SELECT tw.team_id, t.name, tw.computed_points c, tw.posted_points p "
        "FROM team_week_scores tw JOIN teams t ON t.id=tw.team_id "
        "WHERE tw.season_id=? AND tw.ff_week=? AND tw.posted_points IS NOT NULL "
        "AND tw.computed_points IS NOT NULL", (season_id, ff_week)).fetchall()
    mismatches = [{"team": r["name"], "computed": r["c"], "posted": r["p"]}
                  for r in rows if abs(r["c"] - r["p"]) > tol]
    return {"checked": len(rows), "matched": len(rows) - len(mismatches),
            "mismatches": mismatches}
