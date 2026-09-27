"""
Per-player kickoff locks (league rule Q7).

An asset is "locked" for a week once its NFL game has kicked off — after that a
manager can't start or bench it (no swapping in a guy who already played). We
derive kickoff times from the real NFL schedule (nflverse), in US Eastern.

There is no off switch. There used to be — an `enforce_locks` setting, so the
local demo (a finished season) stayed clickable — and because it wasn't tied to
a season, a live database that had once been demo-seeded ran all of FF Week 1
with the league's core rule disabled (Scott, 2026-09-27). A player locks when
his game kicks off, always.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _kickoff(gameday: str, gametime) -> _dt.datetime | None:
    if not gameday:
        return None
    t = str(gametime) if gametime not in (None, "") else "13:00"
    t = t[:5] if len(t) >= 5 else "13:00"          # 'HH:MM' (drop seconds)
    try:
        return _dt.datetime.fromisoformat(f"{gameday}T{t}").replace(tzinfo=ET)
    except ValueError:
        return None


def locked_assets(conn, season_id: int, ff_week: int, now: _dt.datetime | None = None) -> set[str]:
    """Set of asset_refs (NFL team abbrs for units + player gsis_ids) whose game
    this week has already kicked off."""
    from ..data_sources import nflverse as nv
    from .scoring import nfl_week_for

    now = now or _dt.datetime.now(ET)
    year = conn.execute("SELECT year FROM seasons WHERE id=?", (season_id,)).fetchone()["year"]
    nflw = nfl_week_for(conn, season_id, ff_week)
    g = nv.load_games()
    g = g[(g["season"] == year) & (g["week"] == nflw)]

    started_teams = set()
    for _, r in g.iterrows():
        ko = _kickoff(r.get("gameday"), r.get("gametime"))
        if ko is not None and now >= ko:
            started_teams.add(r["home_team"])
            started_teams.add(r["away_team"])

    locked = set(started_teams)                    # team units
    if started_teams:
        marks = ",".join("?" * len(started_teams))
        for r in conn.execute(
            f"SELECT gsis_id FROM nfl_players WHERE season_id=? AND nfl_team_abbr IN ({marks})",
            (season_id, *started_teams)):
            locked.add(r["gsis_id"])
    return locked
