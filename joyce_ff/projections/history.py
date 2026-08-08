"""
Build per-game POINT distributions by scoring every real historical game with
the engine. This is the raw material for valuation.

Individual players (RB / R) and team units (QB / K / DEF / Coach) are handled
separately — different asset classes, scored by different engine functions.
"""

from __future__ import annotations

import pandas as pd

from ..data_sources import nflverse as nv
from ..scoring import engine as E
from ..scoring.models import (CoachUnitGame, DefenseUnitGame, KickerUnitGame,
                              PlayerGame, QBUnitGame)

SEASONS_DEFAULT = (2023, 2024, 2025)


def _pbp(season: int) -> pd.DataFrame:
    p = nv.load_pbp(season)
    return p[p["season_type"].isin(["REG", "POST"])]


def scored_player_games(seasons=SEASONS_DEFAULT) -> pd.DataFrame:
    """One row per (season, week, player) with the engine POINTS for that game
    plus the raw components (so tier-clear rates can be computed later)."""
    frames = []
    for s in seasons:
        pw = nv.player_week_stats(_pbp(s))
        pw["season"] = s
        frames.append(pw)
    allg = pd.concat(frames, ignore_index=True)

    def score(r) -> float:
        return E.score_player_game(PlayerGame(
            player=r["name"], team=r["team"],
            rushing_yards=r.rushing_yards, rushing_tds=r.rushing_tds,
            receiving_yards=r.receiving_yards, receptions=r.receptions,
            receiving_tds=r.receiving_tds, return_tds=r.return_tds)).total

    allg["points"] = allg.apply(score, axis=1)
    return allg


def scored_team_unit_games(seasons=SEASONS_DEFAULT) -> dict[str, pd.DataFrame]:
    """Return {'QB','K','DEF/ST','C'} -> DataFrame of per (season, week, team)
    engine points for that unit."""
    qb_rows, k_rows, d_rows, c_rows = [], [], [], []
    games = nv.load_games()
    for s in seasons:
        p = _pbp(s)
        qb = nv.qb_unit_week_stats(p); qb["season"] = s
        for _, r in qb.iterrows():
            pts = E.score_qb_unit_game(QBUnitGame(team=r.team,
                passing_yards=r.passing_yards, passing_tds=r.passing_tds)).total
            qb_rows.append((s, r.week, r.team, pts))

        kk = nv.kicker_unit_week_stats(p); kk["season"] = s
        for _, r in kk.iterrows():
            pts = E.score_kicker_unit_game(KickerUnitGame(team=r.team,
                field_goal_distances=tuple(r.fg_distances),
                extra_points_made=int(r.extra_points_made))).total
            k_rows.append((s, r.week, r.team, pts))

        dd = nv.defense_unit_week_stats(p, games, s)
        for _, r in dd.iterrows():
            pts = E.score_defense_unit_game(DefenseUnitGame(team=r.team,
                points_allowed=int(r.points_allowed), yards_allowed=int(r.yards_allowed),
                sacks=int(r.sacks), interceptions=int(r.interceptions),
                fumble_recoveries=int(r.fumble_recoveries), safeties=int(r.safeties),
                defensive_tds=int(r.defensive_tds),
                special_teams_tds=int(r.special_teams_tds))).total
            d_rows.append((s, r.week, r.team, pts))

        cc = nv.coach_unit_week_stats(games, s)
        for _, r in cc.iterrows():
            pts = E.score_coach_unit_game(CoachUnitGame(team=r.team,
                won=bool(r.won), tied=bool(r.tied))).total
            c_rows.append((s, r.week, r.team, pts))

    cols = ["season", "week", "team", "points"]
    return {
        "QB": pd.DataFrame(qb_rows, columns=cols),
        "K": pd.DataFrame(k_rows, columns=cols),
        "DEF/ST": pd.DataFrame(d_rows, columns=cols),
        "C": pd.DataFrame(c_rows, columns=cols),
    }
