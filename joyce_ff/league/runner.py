"""
The weekly automation runner — the "it scores itself" command.

run_week() orchestrates the pipeline for one FF week: pull the NFL stats, score
every team from its lineup, run the elimination step, and advance the season.
Each NFL game's stats lock the first time they're complete (scoring.py), so
re-running never moves a finished game's points.

run_current() is what the hourly job calls; its docstring walks through a
week's lifecycle. reconcile_week() compares our computed totals against the
legacy site's posted totals (populated by scrape.py) and flags any disagreement.
"""

from __future__ import annotations

import datetime as _dt

from . import progress, scoring
from . import standings as st

# How long after its last kickoff a week still counts as the one being played:
# long enough to ride out nflverse running days late, short enough that a
# finished season's empty weeks are never filled in after the fact.
ACTIVE_GRACE = _dt.timedelta(days=7)


def carry_forward_lineups(conn, season_id: int, ff_week: int) -> int:
    """Rule: a team that didn't set a lineup keeps last week's — made the way
    the commissioner would make it by hand (see carry.py). Returns how many
    teams got one."""
    from . import carry
    return carry.carry_forward(conn, season_id, ff_week)


def run_week(conn, season_id: int, ff_week: int, *, do_ingest: bool = True,
             eliminate: bool = True, carry: bool = True) -> dict:
    summary: dict = {"ff_week": ff_week}
    if do_ingest:
        summary["assets_scored"] = scoring.ingest_asset_scores_from_nflverse(conn, season_id, ff_week)
    if carry:
        summary["lineups_carried"] = carry_forward_lineups(conn, season_id, ff_week)
    scoring.score_team_week(conn, season_id, ff_week)
    if eliminate:
        # Eliminating is what a FINAL run does, so this week's totals are now
        # results — let standings and the payout count them.
        st.clear_live(conn, season_id, ff_week)
        summary["eliminated_team_ids"] = st.run_elimination(conn, season_id, ff_week)
    conn.execute("UPDATE seasons SET current_ff_week=MAX(current_ff_week, ?) WHERE id=?",
                 (ff_week, season_id))
    conn.commit()
    summary["team_scores"] = {r["team_id"]: r["computed_points"] for r in conn.execute(
        "SELECT team_id, computed_points FROM team_week_scores WHERE season_id=? AND ff_week=?",
        (season_id, ff_week))}
    return summary


def _is_finalized(conn, season_id: int, ff_week: int) -> bool:
    return progress.is_finalized(conn, season_id, ff_week)


def run_current(conn, season_id: int, now: _dt.datetime | None = None) -> dict:
    """Bring every FF week up to date with the NFL games played so far.

    Designed for an hourly schedule on the host. A week's lifecycle:
      * first game kicks off -> any team without a lineup gets last week's,
                                carried forward the way the commissioner would
                                (carry.py). From then on it's their lineup.
      * games in progress    -> score what's in. Each NFL game locks the first
                                time its stats are complete and its points never
                                move again. A matchup whose starters are all
                                locked is final and goes on the records, and the
                                week's elimination is called as soon as it's
                                certain (standings.try_early_elimination).
      * every game locked    -> the final run: the elimination if it hasn't
                                been called yet, the week marked finalized, and
                                later runs skip it.
      * not started          -> skipped.
    """
    from ..data_sources import nflverse as nv
    from . import carry
    from .locks import ET, _kickoff

    now = now or _dt.datetime.now(ET)
    year = conn.execute("SELECT year FROM seasons WHERE id=?", (season_id,)).fetchone()["year"]
    g = nv.load_games()
    g = g[g["season"] == year]
    import pandas as pd

    # nfl week -> (final, total, final ids) / (first, last kickoff) / its games
    played, kicks, slate = {}, {}, {}
    for w, grp in g.groupby("week"):
        fin = grp["home_score"].notna()
        played[int(w)] = (int(fin.sum()), int(len(grp)), set(grp.loc[fin, "game_id"]))
        slate[int(w)] = [(r.get("game_id"), r["home_team"], r["away_team"],
                          _kickoff(r.get("gameday"), r.get("gametime")),
                          pd.notna(r["home_score"]))
                         for r in grp.to_dict("records")]
        kos = [game[3] for game in slate[int(w)] if game[3]]
        if kos:
            kicks[int(w)] = (min(kos), max(kos))

    # The week being played right now: its first game has kicked off and its
    # last isn't long over. Only that week gets lineups carried forward — a
    # past season's empty weeks must never be filled in after the fact.
    active = set()
    for r in conn.execute("SELECT DISTINCT ff_week FROM matchups WHERE season_id=?",
                          (season_id,)).fetchall():
        nflw = scoring.nfl_week_for(conn, season_id, r["ff_week"])
        if nflw in slate:
            progress.store_week_games(conn, season_id, r["ff_week"], slate[nflw])
        k = kicks.get(nflw)
        if k:
            progress.store_kickoffs(conn, season_id, r["ff_week"], *k)
            if k[0] <= now <= k[1] + ACTIVE_GRACE:
                active.add(r["ff_week"])
    conn.commit()

    weeks = sorted(active | {r["ff_week"] for r in conn.execute(
        "SELECT DISTINCT ff_week FROM weekly_lineups WHERE season_id=?", (season_id,))})

    scored, live = [], []
    try:
        for ff in weeks:
            if progress.is_finalized(conn, season_id, ff):
                continue
            if ff in active:
                carry.carry_forward(conn, season_id, ff)      # first kickoff has passed
            if not conn.execute("SELECT 1 FROM weekly_lineups WHERE season_id=? AND ff_week=? "
                                "LIMIT 1", (season_id, ff)).fetchone():
                continue
            n_final, n_games, final_ids = played.get(
                scoring.nfl_week_for(conn, season_id, ff), (0, 0, set()))
            # Final means every score is posted AND every game's stats are in.
            # The schedule alone runs hours ahead of the play-by-play, and
            # finalizing is one-way: it eliminates a team and locks the week.
            if n_games and n_final == n_games and final_ids <= nv.finished_games(year):
                run_week(conn, season_id, ff, do_ingest=True, eliminate=True, carry=True)
                progress.mark_finalized(conn, season_id, ff)
                conn.commit()
                scored.append(ff)
            elif n_final:
                st.mark_live(conn, season_id, ff)
                run_week(conn, season_id, ff, do_ingest=True, eliminate=False, carry=False)
                st.try_early_elimination(conn, season_id, ff, now=now)
                live.append(ff)
    except nv.NotPublishedYet as e:
        # Games have finished but the stats behind them don't exist yet. An
        # empty scoreboard here would look exactly like a scoreboard of zeros,
        # so this has to reach the site as words.
        return _note(conn, season_id, str(e), scored=scored, live=live)

    if not scored and not live and not conn.execute(
            "SELECT 1 FROM weekly_lineups WHERE season_id=? LIMIT 1", (season_id,)).fetchone():
        # Nothing to score isn't the same as nothing happening. Say which.
        return _note(conn, season_id,
                     "No lineups have been submitted yet, so there is nothing to "
                     "score. Set a lineup for at least one team and this will "
                     "start filling in.")

    _clear_note(conn, season_id)
    return {"scored": scored, "live": live, "note": None}


def _note_key(season_id: int) -> str:
    return f"scoring_note:{season_id}"


def _note(conn, season_id: int, msg: str, scored=None, live=None) -> dict:
    """Record why a run produced nothing, for the site to display."""
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                 (_note_key(season_id), msg))
    conn.commit()
    return {"scored": scored or [], "live": live or [], "note": msg}


def _clear_note(conn, season_id: int) -> None:
    conn.execute("DELETE FROM settings WHERE key=?", (_note_key(season_id),))
    conn.commit()


def scoring_note(conn, season_id: int) -> str | None:
    """The last reason scoring produced nothing, or None if all is well."""
    r = conn.execute("SELECT value FROM settings WHERE key=?",
                     (_note_key(season_id),)).fetchone()
    return r["value"] if r else None


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
