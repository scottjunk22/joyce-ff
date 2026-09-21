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

from . import progress, scoring, settle
from . import standings as st

# How long after its last kickoff a week still counts as the one being played:
# long enough to ride out nflverse running days late, short enough that a
# finished season's empty weeks are never filled in after the fact.
ACTIVE_GRACE = _dt.timedelta(days=7)
# How long after a week's last kickoff the hourly job keeps checking nflverse
# against what locked (the Stat check).
CROSS_CHECK_AFTER_FINAL = _dt.timedelta(days=4)


def carry_forward_lineups(conn, season_id: int, ff_week: int) -> int:
    """Rule: a team that didn't set a lineup keeps last week's — made the way
    the commissioner would make it by hand (see carry.py). Returns how many
    teams got one."""
    from . import carry
    return carry.carry_forward(conn, season_id, ff_week)


def run_week(conn, season_id: int, ff_week: int, *, do_ingest: bool = True,
             eliminate: bool = True, carry: bool = True, sources=None) -> dict:
    summary: dict = {"ff_week": ff_week}
    if do_ingest:
        summary["assets_scored"] = scoring.ingest_week(conn, season_id, ff_week,
                                                       sources or scoring.SOURCES)
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


def run_current(conn, season_id: int, now: _dt.datetime | None = None,
                sources=None) -> dict:
    """Bring every FF week up to date with the NFL games played so far.

    Designed for an hourly schedule on the host. A week's lifecycle:
      * first game kicks off -> any team without a lineup gets last week's,
                                carried forward the way the commissioner would
                                (carry.py). From then on it's their lineup.
      * games in progress    -> score what's in (ESPN first, nflverse as backup;
                                scoring.ingest_week). Each NFL game locks the
                                first time a source has it complete, and its
                                points never move again. A matchup whose
                                starters are all locked is final and goes on the
                                records, and the week's elimination is called as
                                soon as it's certain.
      * every game locked    -> the final run: the elimination if it hasn't
                                been called yet, the week marked finalized, and
                                later runs skip it.
      * not started          -> skipped.

    `sources` narrows which feeds are read: the always-on checker passes
    ("espn",) to stay light; the hourly job reads both.
    """
    from ..data_sources import nflverse as nv
    from . import carry
    from .locks import ET, _kickoff

    now = now or _dt.datetime.now(ET)
    year = conn.execute("SELECT year FROM seasons WHERE id=?", (season_id,)).fetchone()["year"]
    if "nflverse" in (sources or scoring.SOURCES):
        # Once a day, the hourly job brings the player pool up to date — before
        # scoring, so today's locks, byes and game times follow a traded player.
        # It must never stop scoring, so a failure is only reported.
        from . import setup
        try:
            res = setup.refresh_nfl_players_daily(conn, season_id, year, now)
            if res and (res["updated"] or res["added"]):
                print(f"  player pool: {res['updated']} updated, {res['added']} added")
        except Exception as e:
            conn.rollback()
            print(f"  player pool refresh skipped: {type(e).__name__}: {e}")
    g = nv.load_games()
    g = g[g["season"] == year]
    import pandas as pd

    # nfl week -> (final, total, final ids) / (first, last kickoff) / its games
    played, kicks, slate = {}, {}, {}
    for w, grp in g.groupby("week"):
        fin = grp["home_score"].notna()
        played[int(w)] = (int(fin.sum()), int(len(grp)), set(grp["game_id"]))
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
            n_final, n_games, all_ids = played.get(
                scoring.nfl_week_for(conn, season_id, ff), (0, 0, set()))
            if ff not in active and not n_final:
                continue                                  # nothing has kicked off
            scoring.ingest_week(conn, season_id, ff, sources or scoring.SOURCES, now=now)
            # Final means every one of the week's games is locked — its stats
            # complete in some source. Finalizing is one-way: it eliminates a
            # team and closes the week.
            if n_games and all_ids <= progress.locked_game_ids(conn, season_id, ff):
                run_week(conn, season_id, ff, do_ingest=False, eliminate=True, carry=True)
                progress.mark_finalized(conn, season_id, ff)
                conn.commit()
                scored.append(ff)
            else:
                st.mark_live(conn, season_id, ff)
                run_week(conn, season_id, ff, do_ingest=False, eliminate=False, carry=False)
                st.try_early_elimination(conn, season_id, ff, now=now)
                live.append(ff)
        if "nflverse" in (sources or scoring.SOURCES):
            # Keep comparing nflverse against what locked for a few days after a
            # week is final: its last game locks from ESPN — closing the week —
            # hours before nflverse has it, so without this the Stat check would
            # never see a week's late games. Only compares and fills blanks;
            # nothing locked is rewritten.
            for r in conn.execute("SELECT DISTINCT ff_week FROM matchups WHERE season_id=?",
                                  (season_id,)).fetchall():
                ff = r["ff_week"]
                k = kicks.get(scoring.nfl_week_for(conn, season_id, ff))
                if (k and progress.is_finalized(conn, season_id, ff)
                        and k[1] <= now <= k[1] + CROSS_CHECK_AFTER_FINAL):
                    try:
                        scoring.ingest_asset_scores_from_nflverse(conn, season_id, ff)
                    except Exception as e:
                        conn.rollback()
                        print(f"  nflverse cross-check for FF week {ff} skipped: {type(e).__name__}: {e}")
        if "espn" in (sources or scoring.SOURCES):
            # Record-keeping for the post-Final settle question (settle.py).
            # It must never stop scoring, so a failure is only reported.
            try:
                settle.run_follow_ups(conn, season_id, now)
            except Exception as e:
                print(f"  settle follow-up skipped: {type(e).__name__}: {e}")
    except nv.NotPublishedYet as e:
        # Games have finished but the stats behind them don't exist yet. An
        # empty scoreboard here would look exactly like a scoreboard of zeros,
        # so this has to reach the site as words.
        return _note(conn, season_id, str(e), scored=scored, live=live)

    if not scored and not live and not conn.execute(
            "SELECT 1 FROM weekly_lineups WHERE season_id=? LIMIT 1", (season_id,)).fetchone():
        # Nothing to score isn't the same as nothing happening. Say which.
        return _note(conn, season_id, NO_LINEUPS)

    _clear_note(conn, season_id)
    return {"scored": scored, "live": live, "note": None}


NO_LINEUPS = ("No lineups have been submitted yet, so there is nothing to score. "
              "Set a lineup for at least one team and this will start filling in.")


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
    if not r:
        return None
    # "No lineups" goes stale the moment a manager saves one, up to an hour
    # before the next run clears it — and then contradicts the cards beneath
    # it. Check it instead of trusting it (Scott, 2026-09-20).
    if r["value"] == NO_LINEUPS and conn.execute(
            "SELECT 1 FROM weekly_lineups WHERE season_id=? LIMIT 1", (season_id,)).fetchone():
        return None
    return r["value"]


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
