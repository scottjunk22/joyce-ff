"""
nflverse play-by-play -> per-game stat lines for the scoring engine.

We derive everything from a single season's play-by-play file because the
2025 per-player weekly release is not published yet, and because PBP is the
authoritative source for the pieces threshold scoring needs that season
aggregates lack: FG distances, per-game defensive box scores, return TDs.

Downloads are cached to data/nflverse_cache/ so the tool works offline after
the first pull. Only pandas/pyarrow are used here; the engine stays pure.

NOTE: These aggregations are transcribed from nflfastR conventions. The
Phase-1 reconciliation against the league site's posted scores is what
confirms they are correct; where a derivation is approximate it is commented.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "nflverse_cache"
PBP_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
           "pbp/play_by_play_{season}.parquet")
GAMES_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
             "schedules/games.csv")
ROSTER_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
              "rosters/roster_{season}.parquet")
_UA = {"User-Agent": "joyce-ff local tool (polite cache; contact league owner)"}


# ---------------------------------------------------------------------------
# Download / cache
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


def load_pbp(season: int) -> pd.DataFrame:
    dest = CACHE_DIR / f"play_by_play_{season}.parquet"
    _download(PBP_URL.format(season=season), dest)
    return pd.read_parquet(dest)


def load_games() -> pd.DataFrame:
    dest = CACHE_DIR / "games.csv"
    _download(GAMES_URL, dest)
    return pd.read_csv(dest)


def load_roster(season: int) -> pd.DataFrame:
    """Season roster: gsis_id (joins to PBP player ids), position, team, name,
    status. Used to define the draftable pool and classify RB vs R (WR/TE)."""
    dest = CACHE_DIR / f"roster_{season}.parquet"
    _download(ROSTER_URL.format(season=season), dest)
    return pd.read_parquet(dest)


# ---------------------------------------------------------------------------
# Player (RB / R) per-game aggregation
# ---------------------------------------------------------------------------

def player_week_stats(pbp: pd.DataFrame) -> pd.DataFrame:
    """Return one row per (week, player_id) with the raw counts the engine
    needs for an individual player. Combines rushing + receiving + passing.
    """
    p = pbp

    rush = (p[p["rusher_player_id"].notna()]
            .groupby(["week", "rusher_player_id"], dropna=True)
            .agg(team=("posteam", "last"),
                 name=("rusher_player_name", "last"),
                 rushing_yards=("rushing_yards", "sum"),
                 rushing_tds=("rush_touchdown", "sum"))
            .reset_index().rename(columns={"rusher_player_id": "player_id"}))

    rec_plays = p[(p["receiver_player_id"].notna()) & (p["complete_pass"] == 1)]
    rec = (rec_plays
           .groupby(["week", "receiver_player_id"], dropna=True)
           .agg(team=("posteam", "last"),
                name=("receiver_player_name", "last"),
                receiving_yards=("receiving_yards", "sum"),
                receptions=("complete_pass", "sum"),
                receiving_tds=("pass_touchdown", "sum"))
           .reset_index().rename(columns={"receiver_player_id": "player_id"}))

    pas = (p[p["passer_player_id"].notna()]
           .groupby(["week", "passer_player_id"], dropna=True)
           .agg(team=("posteam", "last"),
                name=("passer_player_name", "last"),
                passing_yards=("passing_yards", "sum"),
                passing_tds=("pass_touchdown", "sum"))
           .reset_index().rename(columns={"passer_player_id": "player_id"}))

    # Return TDs credited to the scoring player (kick/punt return TDs).
    ret = p[(p["return_touchdown"] == 1) & (p["td_player_id"].notna())]
    ret = (ret.groupby(["week", "td_player_id"], dropna=True)
           .agg(team=("td_team", "last"),
                name=("td_player_name", "last"),
                return_tds=("return_touchdown", "sum"))
           .reset_index().rename(columns={"td_player_id": "player_id"}))

    out = rush
    for frag in (rec, pas, ret):
        out = out.merge(frag, on=["week", "player_id"], how="outer",
                        suffixes=("", "_y"))
        # prefer a non-null name/team
        for col in ("team", "name"):
            if f"{col}_y" in out.columns:
                out[col] = out[col].fillna(out[f"{col}_y"])
                out = out.drop(columns=[f"{col}_y"])

    numeric = ["rushing_yards", "rushing_tds", "receiving_yards", "receptions",
               "receiving_tds", "passing_yards", "passing_tds", "return_tds"]
    for c in numeric:
        if c not in out.columns:
            out[c] = 0
    out[numeric] = out[numeric].fillna(0)
    return out


# ---------------------------------------------------------------------------
# Team-unit per-game aggregation
# ---------------------------------------------------------------------------

def qb_unit_week_stats(pbp: pd.DataFrame) -> pd.DataFrame:
    """Per (week, team) aggregated passing production -> QB slot."""
    p = pbp[pbp["posteam"].notna()]
    return (p.groupby(["week", "posteam"])
            .agg(passing_yards=("passing_yards", "sum"),
                 passing_tds=("pass_touchdown", "sum"))
            .reset_index().rename(columns={"posteam": "team"}))


def kicker_unit_week_stats(pbp: pd.DataFrame) -> pd.DataFrame:
    """Per (week, team): list of made-FG distances + made XP count -> K slot."""
    p = pbp
    fg = p[p["field_goal_result"] == "made"]
    fg_by = (fg.groupby(["week", "posteam"])["kick_distance"]
             .apply(lambda s: [int(x) for x in s.dropna()])
             .reset_index().rename(columns={"posteam": "team",
                                            "kick_distance": "fg_distances"}))
    xp = p[p["extra_point_result"] == "good"]
    xp_by = (xp.groupby(["week", "posteam"]).size()
             .reset_index(name="extra_points_made")
             .rename(columns={"posteam": "team"}))
    out = fg_by.merge(xp_by, on=["week", "team"], how="outer")
    out["fg_distances"] = out["fg_distances"].apply(
        lambda v: v if isinstance(v, list) else [])
    out["extra_points_made"] = out["extra_points_made"].fillna(0).astype(int)
    return out


def defense_unit_week_stats(pbp: pd.DataFrame, games: pd.DataFrame,
                            season: int) -> pd.DataFrame:
    """Per (week, team) DEF/ST box score.

    - points_allowed / opponent identity come from the schedule (final scores).
    - yards_allowed = opponent's offensive yards_gained that game.
    - sacks/INT/safety/def-TD credited to defteam; ST TDs to the returning team.
    """
    p = pbp

    # Offensive yards by team-week (used to derive yards allowed for opponents).
    off_yards = (p[p["posteam"].notna()]
                 .groupby(["week", "posteam"])["yards_gained"].sum()
                 .reset_index().rename(columns={"posteam": "team",
                                                "yards_gained": "off_yards"}))

    d = p[p["defteam"].notna()]
    base = (d.groupby(["week", "defteam"])
            .agg(sacks=("sack", "sum"),
                 interceptions=("interception", "sum"),
                 safeties=("safety", "sum"))
            .reset_index().rename(columns={"defteam": "team"}))

    # Defensive TDs: return TD credited to the defensive team on non-ST plays.
    def_td = p[(p["return_touchdown"] == 1) & (p["td_team"].notna())
               & (p["special_teams_play"] != 1)]
    def_td = (def_td[def_td["td_team"] == def_td["defteam"]]
              .groupby(["week", "td_team"]).size()
              .reset_index(name="defensive_tds").rename(columns={"td_team": "team"}))

    # Special-teams TDs: TD on a special-teams play, credited to the team that
    # scored it (the returning/blocking team's DEF/ST unit).
    st_td = p[(p["special_teams_play"] == 1) & (p["touchdown"] == 1)
              & (p["td_team"].notna())]
    st_td = (st_td.groupby(["week", "td_team"]).size()
             .reset_index(name="special_teams_tds").rename(columns={"td_team": "team"}))

    # Fumble recoveries by the defense: opponent lost a fumble that this team
    # recovered. Approximate via fumble_recovery_1_team == team.
    fr = p[(p["fumble_lost"] == 1) & (p["fumble_recovery_1_team"].notna())]
    fr = (fr.groupby(["week", "fumble_recovery_1_team"]).size()
          .reset_index(name="fumble_recoveries")
          .rename(columns={"fumble_recovery_1_team": "team"}))

    out = base
    for frag in (def_td, st_td, fr):
        out = out.merge(frag, on=["week", "team"], how="left")

    # Points allowed + opponent + yards allowed from the schedule.
    g = games[(games["season"] == season)].copy()
    rows = []
    for _, r in g.iterrows():
        if pd.isna(r["home_score"]) or pd.isna(r["away_score"]):
            continue
        rows.append((r["week"], r["home_team"], r["away_team"], r["away_score"]))
        rows.append((r["week"], r["away_team"], r["home_team"], r["home_score"]))
    sched = pd.DataFrame(rows, columns=["week", "team", "opponent", "points_allowed"])

    out = out.merge(sched, on=["week", "team"], how="right")
    out = out.merge(off_yards.rename(columns={"team": "opponent",
                                              "off_yards": "yards_allowed"}),
                    on=["week", "opponent"], how="left")

    for c in ["sacks", "interceptions", "safeties", "defensive_tds",
              "special_teams_tds", "fumble_recoveries", "yards_allowed"]:
        out[c] = out[c].fillna(0)
    return out


def coach_unit_week_stats(games: pd.DataFrame, season: int) -> pd.DataFrame:
    """Per (week, team) win/tie from final scores."""
    g = games[(games["season"] == season)].copy()
    rows = []
    for _, r in g.iterrows():
        if pd.isna(r["home_score"]) or pd.isna(r["away_score"]):
            continue
        h, a = r["home_score"], r["away_score"]
        rows.append((r["week"], r["home_team"], h > a, h == a))
        rows.append((r["week"], r["away_team"], a > h, h == a))
    return pd.DataFrame(rows, columns=["week", "team", "won", "tied"])
