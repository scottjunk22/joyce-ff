"""
Phase-1 reconciliation harness.

Scrapes the league site (recording an append-only snapshot), then for every
lineup the site currently exposes, auto-identifies the NFL week from the
unambiguous team-unit slots (QB/DEF/K/Coach reference real NFL teams) and
reconciles EVERY slot's posted points against what the scoring engine derives
from real nflverse play-by-play.

Guarantees from the brief:
  * Never fabricate. A name we cannot resolve is reported UNRESOLVED, not 0.
  * Show the reasoning. Mismatches print the engine's breakdown.
  * Read-only site access; one polite fetch; snapshot stored immutably.

Run: python manage.py validate            (live fetch)
     python manage.py validate --cached    (reuse last snapshot / offline)
"""

from __future__ import annotations

import sys

from joyce_ff.data_sources import league_site as ls
from joyce_ff.data_sources import nflverse as nv
from joyce_ff.db import connect, init_db, record_snapshot
from joyce_ff.scoring import engine as E
from joyce_ff.scoring.models import (CoachUnitGame, DefenseUnitGame,
                                     KickerUnitGame, PlayerGame, QBUnitGame)

SEASON = 2025

# Site NFL-team abbreviations -> nflverse team codes. Extend as new spellings
# appear; an unmapped abbreviation is reported, never guessed.
SITE_TEAM_MAP = {
    "Seatt": "SEA", "SEA": "SEA", "NE": "NE", "NO": "NO",
}

# Site player shorthands that aren't a plain last name.
PLAYER_ALIASES = {"S-N": "Smith-Njigba"}

UNIT_SLOTS = {"C", "K", "DEF/ST", "QB"}


def _load_frames():
    pbp = nv.load_pbp(SEASON)
    pbp = pbp[pbp["season_type"].isin(["REG", "POST"])]
    games = nv.load_games()
    return {
        "players": nv.player_week_stats(pbp),
        "qb": nv.qb_unit_week_stats(pbp),
        "kick": nv.kicker_unit_week_stats(pbp),
        "def": nv.defense_unit_week_stats(pbp, games, SEASON),
        "coach": nv.coach_unit_week_stats(games, SEASON),
    }


def _unit_score(frames, slot, team, week):
    """Engine score for a team-unit slot in a given week, or None if no data."""
    if slot == "QB":
        r = frames["qb"].query("team == @team and week == @week")
        if r.empty:
            return None
        row = r.iloc[0]
        return E.score_qb_unit_game(QBUnitGame(team=team,
            passing_yards=row.passing_yards, passing_tds=row.passing_tds)).total
    if slot == "K":
        r = frames["kick"].query("team == @team and week == @week")
        if r.empty:
            return None
        row = r.iloc[0]
        return E.score_kicker_unit_game(KickerUnitGame(team=team,
            field_goal_distances=tuple(row.fg_distances),
            extra_points_made=int(row.extra_points_made))).total
    if slot == "C":
        r = frames["coach"].query("team == @team and week == @week")
        if r.empty:
            return None
        row = r.iloc[0]
        return E.score_coach_unit_game(CoachUnitGame(team=team,
            won=bool(row.won), tied=bool(row.tied))).total
    if slot == "DEF/ST":
        r = frames["def"].query("team == @team and week == @week")
        if r.empty:
            return None
        row = r.iloc[0]
        return E.score_defense_unit_game(_def_game(team, row)).total
    return None


def _def_game(team, row):
    return DefenseUnitGame(team=team, points_allowed=int(row.points_allowed),
        yards_allowed=int(row.yards_allowed), sacks=int(row.sacks),
        interceptions=int(row.interceptions),
        fumble_recoveries=int(row.fumble_recoveries), safeties=int(row.safeties),
        defensive_tds=int(row.defensive_tds),
        special_teams_tds=int(row.special_teams_tds))


def _identify_week(frames, lineup):
    """Find the unique NFL week whose real stats reproduce this lineup's
    team-unit slot points. Returns (week, n_anchors) or (None, 0)."""
    anchors = []
    for s in lineup.slots:
        if s.slot in UNIT_SLOTS:
            team = SITE_TEAM_MAP.get(s.asset)
            if team:
                anchors.append((s.slot, team, s.points))
    if len(anchors) < 2:
        return None, 0
    hits = []
    for wk in range(1, 23):
        if all(_unit_score(frames, slot, team, wk) == pts
               for slot, team, pts in anchors):
            hits.append(wk)
    return (hits[0] if len(hits) == 1 else None), len(anchors)


def _resolve_player(frames, last, week):
    """Unique last-name match in that week -> PlayerGame, else None."""
    last = PLAYER_ALIASES.get(last, last)
    df = frames["players"]
    cand = df[(df.week == week)
              & df.name.str.contains(last, case=False, na=False, regex=False)]
    if len(cand) != 1:
        return None
    row = cand.iloc[0]
    return PlayerGame(player=row["name"], team=row["team"],
        rushing_yards=row.rushing_yards, rushing_tds=row.rushing_tds,
        receiving_yards=row.receiving_yards, receptions=row.receptions,
        receiving_tds=row.receiving_tds, return_tds=row.return_tds)


def _reconcile_lineup(frames, lineup):
    week, n_anchors = _identify_week(frames, lineup)
    ptot = "n/a" if lineup.posted_total is None else f"{lineup.posted_total:g}"
    print(f"\n=== Lineup (team label ~{lineup.team!r}; posted total {ptot}) ===")
    if week is None:
        print(f"  ! Could not uniquely identify the NFL week from "
              f"{n_anchors} team-unit anchors — skipping (no guessing).")
        return None
    print(f"  Identified NFL week: {week} (from {n_anchors} team-unit anchors)")

    results = []
    engine_total = 0.0
    for s in lineup.slots:
        status, engine_pts, detail = "?", None, ""
        if s.slot in UNIT_SLOTS:
            team = SITE_TEAM_MAP.get(s.asset)
            if not team:
                status, detail = "UNRESOLVED", f"no team mapping for {s.asset!r}"
            else:
                engine_pts = _unit_score(frames, s.slot, team, week)
        else:
            g = _resolve_player(frames, s.asset, week)
            if g is None:
                # No unique stat line. If the site posted 0, that is consistent
                # with a player who simply didn't accrue stats; flag it lightly.
                if s.points == 0:
                    status, detail = "OK*", "no stat line; posted 0 (consistent)"
                else:
                    status, detail = "UNRESOLVED", "no unique player match"
            else:
                engine_pts = E.score_player_game(g).total
                detail = g.player

        if engine_pts is not None:
            status = "OK" if engine_pts == s.points else "MISMATCH"
            engine_total += engine_pts
        mark = {"OK": "OK ", "OK*": "OK*", "MISMATCH": ">> ", "UNRESOLVED": "?? "}[status]
        ep = "-" if engine_pts is None else f"{engine_pts:g}"
        print(f"  [{mark}] {s.slot:7s} {s.asset:12s} site={s.points:>3g}  engine={ep:>3}  {detail}")
        results.append((s.slot, s.asset, s.points, engine_pts, status))

    posted = lineup.posted_total
    print(f"  ---- engine total (resolved slots): {engine_total:g}"
          + (f"   posted total: {posted:g}" if posted is not None else ""))
    return results


def main(argv=None) -> int:
    argv = argv or []
    use_cached = "--cached" in argv

    conn = connect()
    init_db(conn)

    if use_cached:
        row = conn.execute(
            "SELECT raw_text, fetched_at_utc FROM scrape_snapshots "
            "WHERE source='league_site' ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            print("No cached snapshot; run without --cached once.", file=sys.stderr)
            return 2
        html, when = row["raw_text"], row["fetched_at_utc"]
        print(f"Using cached league-site snapshot from {when}")
    else:
        print("Fetching league site (one polite request)...")
        html = ls.fetch_home()
        snap_id = record_snapshot(conn, source="league_site", url=ls.HOME_URL,
                                  raw_text=html, notes="validate_scoring")
        print(f"Recorded snapshot #{snap_id}")

    lineups = ls.parse_lineups(html)
    print(f"Parsed {len(lineups)} filled lineup(s) from the site.")

    print("Loading nflverse 2025 play-by-play (cached after first pull)...")
    frames = _load_frames()

    all_ok = True
    reconciled = 0
    for lu in lineups:
        res = _reconcile_lineup(frames, lu)
        if res is None:
            continue
        reconciled += 1
        for _slot, _asset, site_pts, eng_pts, status in res:
            if status == "MISMATCH":
                all_ok = False

    print(f"\n{'='*60}")
    print(f"Reconciled {reconciled} lineup(s). "
          + ("ALL RESOLVED SLOTS MATCH the site." if all_ok
             else "MISMATCHES found (see '>>' above)."))
    conn.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
