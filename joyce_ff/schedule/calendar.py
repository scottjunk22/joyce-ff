"""
FF-week -> NFL-week -> calendar-date mapping, per season.

The matchup rotation (rotation.py) says WHO plays whom; this says WHEN. In
2025-26 the league skipped the first two NFL weeks (FF "pre-season") and ran
FF Weeks 1-15 over NFL Weeks 3-17, with NFL Week 18 a bye ("No Play").

We assume the same offset for 2026-27 (FF Week 1 = NFL Week 3) and pull real
NFL week dates from nflverse. The offset is a league convention, so it is
surfaced as an ASSUMPTION to confirm for each new season, never hard-guessed.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..data_sources import nflverse as nv
from .rotation import NO_PLAY_WEEK

# FF Week 1 falls on this NFL week (2025-26 convention). Confirm per season.
FF_START_NFL_WEEK = 3


def ff_to_nfl_week(ff_week: int) -> int:
    return ff_week + (FF_START_NFL_WEEK - 1)


def _fmt_safe(d) -> str:
    # Format a YYYY-MM-DD date as e.g. "Sep 24" (Windows strftime lacks %-d).
    dt = datetime.strptime(str(d)[:10], "%Y-%m-%d")
    return f"{dt.strftime('%b')} {dt.day}"


def season_calendar(season: int) -> dict[int, dict]:
    """Return {ff_week -> {nfl_week, start, end, label}} for FF weeks 1-16,
    using real NFL week date ranges from nflverse schedules."""
    games = nv.load_games()
    s = games[games["season"] == season]
    wk_dates = {}
    for wk, grp in s.groupby("week"):
        days = grp["gameday"].dropna().astype(str)
        if len(days):
            wk_dates[int(wk)] = (days.min(), days.max())

    out = {}
    for ff in range(1, NO_PLAY_WEEK + 1):
        nfl = ff_to_nfl_week(ff)
        rng = wk_dates.get(nfl)
        if rng:
            label = f"{_fmt_safe(rng[0])} – {_fmt_safe(rng[1])}"
        else:
            label = "TBD"
        out[ff] = {"nfl_week": nfl, "start": rng[0] if rng else None,
                   "end": rng[1] if rng else None, "label": label}
    return out
