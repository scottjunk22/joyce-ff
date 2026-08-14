"""
Weekly scoring pipeline.

Two layers, split so the pure DB logic is testable without network:
  * ingest_asset_scores_from_nflverse() — pulls real stats for the NFL week,
    scores every player/unit with the validated engine, stores per-asset lines
    (with breakdown) in asset_week_scores.
  * score_team_week() / box_score() — pure DB: sum a team's started assets
    from its lineup, using the stored asset scores. No network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def nfl_week_for(conn, season_id: int, ff_week: int) -> int:
    row = conn.execute("SELECT ff_start_nfl_week FROM seasons WHERE id=?", (season_id,)).fetchone()
    start = row["ff_start_nfl_week"] if row else 3
    return ff_week + (start - 1)


def _upsert_asset(conn, season_id, ff_week, kind, ref, unit, breakdown):
    # unit_type is part of the key (one NFL team = 4 units); players use ''.
    conn.execute(
        "INSERT INTO asset_week_scores(season_id,ff_week,asset_kind,asset_ref,unit_type,"
        "points,breakdown_json,computed_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(season_id,ff_week,asset_kind,asset_ref,unit_type) DO UPDATE SET "
        "points=excluded.points, breakdown_json=excluded.breakdown_json, "
        "computed_at=excluded.computed_at",
        (season_id, ff_week, kind, ref, unit or "", breakdown.total,
         json.dumps(breakdown.items), _now()))


def ingest_asset_scores_from_nflverse(conn, season_id: int, ff_week: int) -> int:
    """Score every player + team unit for the NFL week behind this FF week and
    store them. Returns the number of asset lines written. Needs nflverse data.
    """
    from ..data_sources import nflverse as nv
    from ..scoring import engine as E
    from ..scoring.models import (CoachUnitGame, DefenseUnitGame, KickerUnitGame,
                                  PlayerGame, QBUnitGame)

    year = conn.execute("SELECT year FROM seasons WHERE id=?", (season_id,)).fetchone()["year"]
    wk = nfl_week_for(conn, season_id, ff_week)
    pbp = nv.load_pbp(year)
    pbp = pbp[pbp["season_type"].isin(["REG", "POST"])]
    games = nv.load_games()
    n = 0

    pw = nv.player_week_stats(pbp)
    for _, r in pw[pw["week"] == wk].iterrows():
        b = E.score_player_game(PlayerGame(
            player=r["name"], team=r["team"], rushing_yards=r.rushing_yards,
            rushing_tds=r.rushing_tds, receiving_yards=r.receiving_yards,
            receptions=r.receptions, receiving_tds=r.receiving_tds, return_tds=r.return_tds))
        _upsert_asset(conn, season_id, ff_week, "PLAYER", r["player_id"], None, b); n += 1

    qb = nv.qb_unit_week_stats(pbp)
    for _, r in qb[qb["week"] == wk].iterrows():
        b = E.score_qb_unit_game(QBUnitGame(team=r.team, passing_yards=r.passing_yards,
                                            passing_tds=r.passing_tds))
        _upsert_asset(conn, season_id, ff_week, "TEAM_UNIT", r.team, "QB", b); n += 1

    kk = nv.kicker_unit_week_stats(pbp)
    for _, r in kk[kk["week"] == wk].iterrows():
        b = E.score_kicker_unit_game(KickerUnitGame(team=r.team,
            field_goal_distances=tuple(r.fg_distances), extra_points_made=int(r.extra_points_made)))
        _upsert_asset(conn, season_id, ff_week, "TEAM_UNIT", r.team, "K", b); n += 1

    dd = nv.defense_unit_week_stats(pbp, games, year)
    for _, r in dd[dd["week"] == wk].iterrows():
        b = E.score_defense_unit_game(DefenseUnitGame(team=r.team,
            points_allowed=int(r.points_allowed), yards_allowed=int(r.yards_allowed),
            sacks=int(r.sacks), interceptions=int(r.interceptions),
            fumble_recoveries=int(r.fumble_recoveries), safeties=int(r.safeties),
            defensive_tds=int(r.defensive_tds), special_teams_tds=int(r.special_teams_tds)))
        _upsert_asset(conn, season_id, ff_week, "TEAM_UNIT", r.team, "DEF/ST", b); n += 1

    cc = nv.coach_unit_week_stats(games, year)
    for _, r in cc[cc["week"] == wk].iterrows():
        b = E.score_coach_unit_game(CoachUnitGame(team=r.team, won=bool(r.won), tied=bool(r.tied)))
        _upsert_asset(conn, season_id, ff_week, "TEAM_UNIT", r.team, "C", b); n += 1

    conn.commit()
    return n


def score_team_week(conn, season_id: int, ff_week: int) -> None:
    """Sum each team's started assets (from weekly_lineups) using stored asset
    scores, and upsert team_week_scores.computed_points."""
    teams = [r["team_id"] for r in conn.execute(
        "SELECT DISTINCT team_id FROM weekly_lineups WHERE season_id=? AND ff_week=?",
        (season_id, ff_week))]
    for team_id in teams:
        total = conn.execute(
            "SELECT COALESCE(SUM(a.points),0) s FROM weekly_lineups l "
            "JOIN asset_week_scores a ON a.season_id=l.season_id AND a.ff_week=l.ff_week "
            "AND a.asset_kind=l.asset_kind AND a.asset_ref=l.asset_ref "
            "AND (l.asset_kind='PLAYER' OR a.unit_type=l.roster_slot) "
            "WHERE l.season_id=? AND l.ff_week=? AND l.team_id=?",
            (season_id, ff_week, team_id)).fetchone()["s"]
        conn.execute(
            "INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points,computed_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(season_id,team_id,ff_week) DO UPDATE SET "
            "computed_points=excluded.computed_points, computed_at=excluded.computed_at "
            "WHERE team_week_scores.adjusted=0",   # never clobber a commissioner override
            (season_id, team_id, ff_week, total, _now()))
    conn.commit()


def box_score(conn, season_id: int, ff_week: int, team_id: int) -> list[dict]:
    """The team's starters with their points + breakdown, for the box-score UI."""
    rows = conn.execute(
        "SELECT l.roster_slot, l.asset_kind, l.asset_ref, l.unit_type, l.is_rental, "
        "COALESCE(a.points,0) points, a.breakdown_json, p.name player_name "
        "FROM weekly_lineups l "
        "LEFT JOIN asset_week_scores a ON a.season_id=l.season_id AND a.ff_week=l.ff_week "
        "AND a.asset_kind=l.asset_kind AND a.asset_ref=l.asset_ref "
        "AND (l.asset_kind='PLAYER' OR a.unit_type=l.roster_slot) "
        "LEFT JOIN nfl_players p ON p.season_id=l.season_id AND p.gsis_id=l.asset_ref "
        "WHERE l.season_id=? AND l.ff_week=? AND l.team_id=? "
        "ORDER BY CASE l.roster_slot WHEN 'C' THEN 0 WHEN 'K' THEN 1 WHEN 'DEF/ST' THEN 2 "
        "WHEN 'QB' THEN 3 WHEN 'RB' THEN 4 WHEN 'R' THEN 5 ELSE 6 END, l.id",
        (season_id, ff_week, team_id))
    out = []
    for r in rows:
        d = dict(r)
        d["display"] = d.pop("player_name") or f"{d['asset_ref']} {d['unit_type'] or ''}".strip()
        d["breakdown"] = json.loads(d.pop("breakdown_json") or "[]")
        out.append(d)
    return out
