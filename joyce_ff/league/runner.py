"""
The weekly automation runner — the "it scores itself" command.

run_week() orchestrates the pipeline for one FF week: pull the NFL stats, score
every team from its submitted lineup, run the elimination step, and advance the
season. Idempotent — safe to re-run when a stat corrects.

reconcile_week() compares our computed totals against the legacy site's posted
totals (populated by scrape.py) and flags any disagreement.
"""

from __future__ import annotations

from . import scoring
from . import standings as st


def run_week(conn, season_id: int, ff_week: int, *, do_ingest: bool = True,
             eliminate: bool = True) -> dict:
    summary: dict = {"ff_week": ff_week}
    if do_ingest:
        summary["assets_scored"] = scoring.ingest_asset_scores_from_nflverse(conn, season_id, ff_week)
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
