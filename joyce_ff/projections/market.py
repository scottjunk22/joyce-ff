"""
Market-inefficiency analysis.

The other 21 managers almost certainly draft on STANDARD fantasy instinct
(linear yardage, 1 pt/reception PPR, standard TDs, ~12-team replacement). This
module builds that shadow valuation from the same nflverse history, then diffs
it against our threshold / 22-team board. The gaps are the edges:

  * players we rank MUCH higher than standard  -> value that falls to us
  * players standard ranks higher than we do   -> what the room will overdraft
  * whole asset classes standard misprices     -> kickers, DEF, and coaches
    (coaches don't even exist in standard fantasy)

We compare per-game RANKS within each slot, which isolates the scoring
difference cleanly. The 22-vs-12-team replacement gap is a second, additive
effect (deep sleepers matter far more here) noted in the report.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..data_sources import nflverse as nv
from . import history
from . import valuation as val

# Standard full-PPR, linear scoring — the "public perception" baseline.
STD_PPR = 1.0
STD_YARD = 0.1        # 1 pt / 10 yards, rushing & receiving
STD_TD = 6.0


def standard_points(df: pd.DataFrame) -> pd.Series:
    return (df["rushing_yards"] * STD_YARD
            + df["receiving_yards"] * STD_YARD
            + df["receptions"] * STD_PPR
            + (df["rushing_tds"] + df["receiving_tds"]
               + df.get("return_tds", 0)) * STD_TD)


def _wmean(df, valcol, weights):
    d = df.copy()
    d["w"] = d["season"].map(weights).fillna(min(weights.values()))
    g = d.groupby("player_id")
    num = (d[valcol] * d["w"]).groupby(d["player_id"]).sum()
    den = d["w"].groupby(d["player_id"]).sum()
    return (num / den)


def build_comparison(seasons=history.SEASONS_DEFAULT, draft_season=2026,
                     weights=val.RECENCY_WEIGHTS, min_games=6) -> pd.DataFrame:
    sp = history.scored_player_games(seasons)          # 'points' = Joyce scoring
    sp["std_points"] = standard_points(sp)

    ours = _wmean(sp, "points", weights).rename("our_ppg")
    std = _wmean(sp, "std_points", weights).rename("std_ppg")
    g = sp.groupby("player_id")
    meta = pd.DataFrame({
        "games": g.size(),
        "td_pg": (g["rushing_tds"].sum() + g["receiving_tds"].sum()) / g.size(),
        "rec_pg": g["receptions"].sum() / g.size(),
        "bust": g["points"].apply(lambda s: float((s == 0).mean())),
    })
    tbl = pd.concat([ours, std, meta], axis=1).reset_index()

    roster = nv.load_roster(draft_season).rename(columns={"gsis_id": "player_id"})
    roster = roster[roster["position"].isin(val.POS_TO_SLOT)]
    roster = roster[["player_id", "full_name", "position", "team"]].drop_duplicates("player_id")
    roster["slot"] = roster["position"].map(val.POS_TO_SLOT)
    tbl = roster.merge(tbl, on="player_id", how="inner")

    tbl = tbl[tbl["games"] >= min_games].copy()
    # Rank within slot under each scoring (1 = best).
    for scope, col in (("our_rank", "our_ppg"), ("std_rank", "std_ppg")):
        tbl[scope] = tbl.groupby("slot")[col].rank(ascending=False, method="min")
    tbl["rank_delta"] = tbl["std_rank"] - tbl["our_rank"]   # >0 => we like more
    return tbl


# "Startable" cutoffs, scaled to the 11-team DIVISION draft (separate pools).
# STD_STARTABLE ~ the players the room actually starts (division started
# counts); OUR_SOLID is a tighter "solid starter" line so a big rank gap
# reflects a real value, not a fringe/replacement-level player.
STD_STARTABLE = dict(val.STARTED)                        # {'RB': 22, 'R': 33}
OUR_SOLID = {s: max(1, round(n * 0.75)) for s, n in val.STARTED.items()}  # ~16 / ~25


def _tag(row) -> str:
    tags = []
    if row["td_pg"] >= 0.5:
        tags.append("TD-heavy")
    if row["rec_pg"] >= 4.5 and row["td_pg"] < 0.5:
        tags.append("PPR-volume")
    if row["bust"] <= 0.22:
        tags.append("steady/low-bust")
    if row["td_pg"] < 0.3 and row["bust"] >= 0.4:
        tags.append("boom-bust")
    return ", ".join(tags) or "-"


def edges(cmp: pd.DataFrame, n=12):
    """Return (falls_to_us, market_overvalues) DataFrames.

    falls_to_us : players WE consider startable that the standard room ranks
                  well below — value that slides to us.
    overvalues  : players the standard room drafts as startable that WE rank
                  well lower — the picks to let them have.
    """
    falls_parts, over_parts = [], []
    for slot in ("RB", "R"):
        s = cmp[cmp["slot"] == slot]
        f = s[(s["our_rank"] <= OUR_SOLID[slot]) & (s["rank_delta"] > 0)]
        falls_parts.append(f)
        o = s[(s["std_rank"] <= STD_STARTABLE[slot]) & (s["rank_delta"] < 0)]
        over_parts.append(o)
    falls = pd.concat(falls_parts).sort_values("rank_delta", ascending=False).head(n).copy()
    over = pd.concat(over_parts).sort_values("rank_delta").head(n).copy()
    for d in (falls, over):
        d["why"] = d.apply(_tag, axis=1)
    return falls, over
