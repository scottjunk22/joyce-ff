"""
Strength of schedule, measured in THIS league's scoring.

Public SoS ratings are built on yards and PPR points allowed. Under threshold
scoring what matters is how often a defense lets a back clear 75 yards or reach
the end zone — a unit that bends for 60-yard games is brutal for us and looks
average everywhere else. So we rate defences by the engine points they actually
surrender to each slot, and we only count the NFL weeks our season covers
(FF weeks 1-15 = NFL weeks 3-17 by default), not all 18.

SoS is a tiebreaker, not a ranking. Pre-season it leans on last season's
defences, which change; it earns its keep once the current season has games.
"""

from __future__ import annotations

import pandas as pd

from ..data_sources import nflverse as nv

SLOTS = ("RB", "R")
POS_TO_SLOT = {"RB": "RB", "WR": "R", "TE": "R"}

# A game in the season being drafted counts this much more than one from the
# prior season: last year's defence is a different unit, so current evidence
# takes over as it accumulates (2 games ~ a nudge, 8 games ~ dominant).
CURRENT_SEASON_WEIGHT = 3.0

# Week 18 is excluded from the defensive ratings. Not because it scores low —
# a league-wide dip cancels out in a ratio-to-average — but because it's
# UNEVENLY low: whether a defence drew a resting, playoff-locked team is decided
# by the opponent's seeding. Across 2024-25 the per-defence deviation in week 18
# is sd 4.8 (range -6.3 to +14.1) against 2.2 in weeks 1-2, and it lands in just
# ONE game per defence per season, so the noise goes straight into the rating.
# Weeks 1-2 stay: their dip is broadly shared, spread over two games, and in the
# season being drafted they are the only current evidence that exists.
MEASURE_EXCLUDE_WEEKS = (18,)


def _opponent_map(games: pd.DataFrame, season: int) -> dict:
    """(week, team) -> opponent, regular season only."""
    g = games[(games["season"] == season) & (games["game_type"] == "REG")]
    m = {}
    for _, r in g.iterrows():
        wk, home, away = int(r["week"]), r["home_team"], r["away_team"]
        m[(wk, home)] = away
        m[(wk, away)] = home
    return m


def _slot_map(season: int) -> dict:
    """player_id -> slot for one season's roster."""
    ros = nv.load_roster(season)
    ros = ros[ros["position"].isin(POS_TO_SLOT)]
    return {str(r["gsis_id"]): POS_TO_SLOT[r["position"]]
            for _, r in ros.iterrows() if r.get("gsis_id")}


def points_allowed(scored: pd.DataFrame, games: pd.DataFrame, seasons: list[int],
                   exclude_weeks: tuple = MEASURE_EXCLUDE_WEEKS) -> pd.DataFrame:
    """One row per (season, week, defense, slot): engine points that defence
    gave up to that slot in that game. Postseason drops out on its own — the
    opponent map is regular-season only."""
    frames = []
    for s in seasons:
        sub = scored[(scored["season"] == s) & (~scored["week"].isin(exclude_weeks))]
        if sub.empty:
            continue
        opp = _opponent_map(games, s)
        slots = _slot_map(s)
        d = sub.copy()
        d["slot"] = d["player_id"].astype(str).map(slots)
        d = d[d["slot"].notna()]
        d["defense"] = [opp.get((int(w), t)) for w, t in zip(d["week"], d["team"])]
        d = d[d["defense"].notna()]
        frames.append(d.groupby(["season", "week", "defense", "slot"], as_index=False)
                       ["points"].sum().rename(columns={"points": "allowed"}))
    if not frames:
        return pd.DataFrame(columns=["season", "week", "defense", "slot", "allowed"])
    return pd.concat(frames, ignore_index=True)


def defense_ratings(allowed: pd.DataFrame, current_season: int,
                    current_weight: float = CURRENT_SEASON_WEIGHT) -> dict:
    """{slot: {team: rating}} where 1.00 = league average, >1 = generous
    (an easier matchup), <1 = stingy. Also returns games behind each rating."""
    out, meta = {}, {}
    for slot in SLOTS:
        sub = allowed[allowed["slot"] == slot]
        if sub.empty:
            continue
        sub = sub.copy()
        sub["w"] = sub["season"].map(lambda s: current_weight if s == current_season else 1.0)
        sub["wa"] = sub["allowed"] * sub["w"]
        g = sub.groupby("defense").agg(num=("wa", "sum"), den=("w", "sum"),
                                       games=("allowed", "size"))
        g["mean_allowed"] = g["num"] / g["den"]
        league = float(g["mean_allowed"].mean())
        if league <= 0:
            continue
        out[slot] = {t: float(v / league) for t, v in g["mean_allowed"].items()}
        meta[slot] = {"league_avg": round(league, 2),
                      "teams": int(len(g)), "games": int(g["games"].sum())}
    return {"ratings": out, "basis": meta}


def season_sos(games: pd.DataFrame, season: int, ratings: dict,
               ff_start_nfl_week: int = 3, ff_weeks: int = 15) -> dict:
    """{slot: {team: sos}} over the NFL weeks our FF season actually covers.
    A team's bye simply contributes no game."""
    first = ff_start_nfl_week
    last = ff_start_nfl_week + ff_weeks - 1
    opp = _opponent_map(games, season)
    teams = sorted({t for (_, t) in opp})
    out = {}
    for slot, rate in ratings.items():
        vals = {}
        for t in teams:
            rs = [rate[o] for wk in range(first, last + 1)
                  if (o := opp.get((wk, t))) is not None and o in rate]
            if rs:
                vals[t] = round(sum(rs) / len(rs), 4)
        out[slot] = vals
    return out


def build(scored_players: pd.DataFrame, draft_season: int,
          ff_start_nfl_week: int = 3, ff_weeks: int = 15,
          lookback: int = 1) -> dict:
    """Full SoS payload for the board.

    Uses the season being drafted (once it has games) plus `lookback` prior
    seasons for the defensive ratings, then applies them to that season's
    remaining schedule. Returns empty structures rather than raising if the
    data isn't there — a missing source shows as blank cells, never a guess.
    """
    games = nv.load_games()
    seasons = [draft_season - i for i in range(lookback, -1, -1)]
    allowed = points_allowed(scored_players, games, seasons)
    if allowed.empty:
        return {"ratings": {}, "sos": {}, "basis": {}, "weeks": [], "seasons": seasons}
    rated = defense_ratings(allowed, draft_season)
    sos = season_sos(games, draft_season, rated["ratings"],
                     ff_start_nfl_week, ff_weeks)
    played = sorted(allowed[allowed["season"] == draft_season]["week"].unique().tolist())
    return {"ratings": rated["ratings"], "sos": sos, "basis": rated["basis"],
            "weeks": [ff_start_nfl_week, ff_start_nfl_week + ff_weeks - 1],
            "seasons": seasons, "current_games_weeks": [int(w) for w in played]}
