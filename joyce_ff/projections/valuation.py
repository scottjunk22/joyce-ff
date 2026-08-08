"""
Turn per-game score distributions into draft-time valuations.

Key choices (all defensible, all visible so you can disagree):
  * Projection = recency-weighted MEAN of per-game engine points. Because we
    average points (already threshold-scored), boom-bust players are correctly
    penalized — a 40/120 back scores less than a steady-80 back.
  * Replacement level is computed from THIS league's 22-team settings, not
    public rankings. VOR = projection - replacement projection.
  * We never invent a projection. A 2026 rookie / no-history player gets NaN
    and is surfaced as "no history", never a made-up number.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..scoring import rules

# Weight recent seasons more. A game's weight is set by its season.
RECENCY_WEIGHTS = {2025: 1.0, 2024: 0.6, 2023: 0.35}

# Replacement cutoffs from the league's 22-team roster math (rules.py).
ROSTERED = {"RB": rules.ROSTERED_RB_LEAGUEWIDE,   # 66
            "R": rules.ROSTERED_R_LEAGUEWIDE}      # 88
STARTED = {"RB": rules.STARTED_RB_LEAGUEWIDE,      # 44
           "R": rules.STARTED_R_LEAGUEWIDE}        # 66
UNITS_OWNED = rules.NUM_TEAMS                        # 22 of ~32

POS_TO_SLOT = {"RB": "RB", "WR": "R", "TE": "R"}


def _reduce(df: pd.DataFrame, key: str, weights: dict) -> pd.DataFrame:
    """Reduce a per-game points table to per-`key` projection + distribution
    stats using a recency-weighted mean."""
    d = df.copy()
    d["w"] = d["season"].map(weights).fillna(min(weights.values()))
    d["wp"] = d["points"] * d["w"]
    g = d.groupby(key)
    out = g.agg(
        proj_w_num=("wp", "sum"),
        proj_w_den=("w", "sum"),
        games=("points", "size"),
        mean=("points", "mean"),
        std=("points", "std"),
        p25=("points", lambda s: float(np.percentile(s, 25))),
        p75=("points", lambda s: float(np.percentile(s, 75))),
    )
    out["proj_raw"] = out["proj_w_num"] / out["proj_w_den"]
    out["eff_games"] = out["proj_w_den"]      # sum of recency weights
    out["bust_rate"] = g.apply(lambda x: float((x["points"] == 0).mean()),
                               include_groups=False)
    g2025 = d[d["season"] == 2025].groupby(key).size()
    out["games_2025"] = g2025
    out["games_2025"] = out["games_2025"].fillna(0).astype(int)
    return out.reset_index()


# Pseudo-games of prior belief for shrinkage. A player with few real games is
# pulled toward the prior; a full-season sample barely moves.
SHRINK_K = 6.0


def _shrink(num: pd.Series, den: pd.Series, prior: float,
            k: float = SHRINK_K) -> pd.Series:
    """Empirical-Bayes shrink: (weighted_points + k*prior) / (weight + k)."""
    return (num + k * prior) / (den + k)


def player_board(scored_players: pd.DataFrame, roster_pool: pd.DataFrame,
                 weights: dict = RECENCY_WEIGHTS, min_games: int = 4) -> pd.DataFrame:
    """Individual RB / R draft board with VOR, restricted to the draftable
    2026 pool. Players with too little history are kept but flagged.
    """
    reduced = _reduce(scored_players, "player_id", weights)

    pool = roster_pool.rename(columns={"gsis_id": "player_id"}).copy()
    pool = pool[pool["position"].isin(POS_TO_SLOT)]
    pool["slot"] = pool["position"].map(POS_TO_SLOT)
    keep = ["player_id", "full_name", "position", "slot", "team", "status"]
    pool = pool[keep].drop_duplicates("player_id")

    board = pool.merge(reduced, on="player_id", how="left")
    board["name"] = board["full_name"]
    board["low_sample"] = board["games"].fillna(0) < min_games
    board["no_history"] = board["proj_raw"].isna()

    # Shrink each slot's projections toward that slot's replacement level
    # (a conservative prior: with little evidence, assume freely-available
    # value). This stops 1-game outliers from ranking like stars.
    board["proj"] = np.nan
    board["prior"] = np.nan
    for slot, cutoff in ROSTERED.items():
        sub = (board[(board["slot"] == slot) & board["proj_raw"].notna()]
               .sort_values("proj_raw", ascending=False))
        if sub.empty:
            continue
        prior = float(sub["proj_raw"].iloc[min(cutoff, len(sub)) - 1])
        m = (board["slot"] == slot) & board["proj_raw"].notna()
        board.loc[m, "prior"] = prior
        board.loc[m, "proj"] = _shrink(board.loc[m, "proj_w_num"],
                                       board.loc[m, "proj_w_den"], prior)

    board = _add_vor(board)
    board = _add_tiers(board)
    sort_cols = ["slot", "vor"]
    return board.sort_values(sort_cols, ascending=[True, False],
                             na_position="last").reset_index(drop=True)


def _add_vor(board: pd.DataFrame) -> pd.DataFrame:
    board["vor"] = np.nan
    board["repl_level"] = np.nan
    for slot, cutoff in ROSTERED.items():
        sub = (board[(board["slot"] == slot) & board["proj"].notna()]
               .sort_values("proj", ascending=False))
        if sub.empty:
            continue
        idx = min(cutoff, len(sub)) - 1
        repl = float(sub["proj"].iloc[idx])
        board.loc[sub.index, "repl_level"] = repl
        board.loc[sub.index, "vor"] = board.loc[sub.index, "proj"] - repl
    return board


def _add_tiers(board: pd.DataFrame, gap_mult: float = 1.8) -> pd.DataFrame:
    """Tier by VOR cliffs within each slot: a new tier starts where the drop to
    the next player is unusually large (> gap_mult x the median drop)."""
    board["tier"] = np.nan
    for slot in board["slot"].dropna().unique():
        sub = (board[(board["slot"] == slot) & board["vor"].notna()]
               .sort_values("vor", ascending=False))
        if len(sub) < 2:
            board.loc[sub.index, "tier"] = 1
            continue
        vals = sub["vor"].to_numpy()
        drops = -np.diff(vals)                 # positive gaps between neighbours
        med = np.median(drops[drops > 0]) if np.any(drops > 0) else 0.0
        tier, tiers = 1, [1]
        for dgap in drops:
            if med > 0 and dgap > gap_mult * med:
                tier += 1
            tiers.append(tier)
        board.loc[sub.index, "tier"] = tiers
    return board


def team_unit_board(unit_games: dict[str, pd.DataFrame],
                    weights: dict = RECENCY_WEIGHTS) -> dict[str, pd.DataFrame]:
    """Per-unit (QB/K/DEF-ST/C) team boards with VOR vs the first unowned team
    (~22 of 32 owned)."""
    boards = {}
    for unit, df in unit_games.items():
        red = _reduce(df, "team", weights).sort_values("proj_raw", ascending=False)
        prior = float(red["proj_raw"].iloc[min(UNITS_OWNED, len(red)) - 1])
        red["prior"] = prior
        red["proj"] = _shrink(red["proj_w_num"], red["proj_w_den"], prior)
        red = red.sort_values("proj", ascending=False)
        repl = float(red["proj"].iloc[min(UNITS_OWNED, len(red)) - 1])
        red["repl_level"] = repl
        red["vor"] = red["proj"] - repl
        red["unit"] = unit
        boards[unit] = red.reset_index(drop=True)
    return boards
