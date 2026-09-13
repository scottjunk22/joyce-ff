"""
Where a fantasy week stands, worked out from the database alone — no network,
so the site can ask on every page load.

Three facts drive it:
  * which NFL games are LOCKED — their stats are complete and frozen
    (nfl_game_locks, written by scoring.py the first time a game's play-by-play
    reaches END GAME with its final score posted);
  * which NFL teams are on bye this week (nfl_teams.bye_ff_week);
  * when the week's first and last games kick off (stored by the hourly
    runner, which is the part that reads the schedule).

From those, each fantasy team this week is:
  * done  — every starter's game is locked and nothing can still change the
            total. A starter on bye never locks (he has no game), and he could
            still be swapped out or covered by an Open until the week's last
            kickoff, so a team carrying one isn't done until then.
  * floor — the points already locked in. No slot in this league scores
            negative, so a team can never finish below its floor. That's what
            makes calling a matchup, or an elimination, before Monday night safe.

A week that has been finalized — or was scored before any of this existed and
isn't flagged in progress — is settled: every team in it is done.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# --- week-level flags (settings) --------------------------------------------

def _final_key(season_id: int, ff_week: int) -> str:
    return f"week_final:{season_id}:{ff_week}"


def is_finalized(conn, season_id: int, ff_week: int) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (_final_key(season_id, ff_week),)).fetchone()
    return bool(row) and row["value"] == "1"


def mark_finalized(conn, season_id: int, ff_week: int) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,'1')",
                 (_final_key(season_id, ff_week),))


# A week the hourly runner is scoring while its games are still going is
# flagged IN PROGRESS until its final run. Its running totals aren't results.
# The flag marks in-progress weeks rather than finished ones on purpose: weeks
# scored before it existed, or by the run-week command, carry no flag and keep
# counting exactly as they always have.

def _live_key(season_id: int, ff_week: int) -> str:
    return f"week_live:{season_id}:{ff_week}"


def mark_live(conn, season_id: int, ff_week: int) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,'1')",
                 (_live_key(season_id, ff_week),))


def clear_live(conn, season_id: int, ff_week: int) -> None:
    conn.execute("DELETE FROM settings WHERE key=?", (_live_key(season_id, ff_week),))


def live_weeks(conn, season_id: int) -> set[int]:
    prefix = f"week_live:{season_id}:"
    return {int(r["key"][len(prefix):]) for r in conn.execute(
        "SELECT key FROM settings WHERE key LIKE ?", (prefix + "%",))}


def _kick_key(season_id: int, ff_week: int) -> str:
    return f"kickoffs:{season_id}:{ff_week}"


def store_kickoffs(conn, season_id: int, ff_week: int,
                   first: _dt.datetime, last: _dt.datetime) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                 (_kick_key(season_id, ff_week), f"{first.isoformat()}|{last.isoformat()}"))


def kickoffs(conn, season_id: int, ff_week: int):
    """(first, last) kickoff of the week, or (None, None) if the runner hasn't
    recorded them yet."""
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (_kick_key(season_id, ff_week),)).fetchone()
    if not row:
        return None, None
    first, _, last = row["value"].partition("|")
    return _dt.datetime.fromisoformat(first), _dt.datetime.fromisoformat(last)


# --- games --------------------------------------------------------------------

def locked_teams(conn, season_id: int, ff_week: int) -> set[str]:
    """NFL teams whose game this week is locked."""
    out: set[str] = set()
    for r in conn.execute("SELECT home_team, away_team FROM nfl_game_locks "
                          "WHERE season_id=? AND ff_week=?", (season_id, ff_week)):
        out.update((r["home_team"], r["away_team"]))
    return out


def bye_teams(conn, season_id: int, ff_week: int) -> set[str]:
    return {r["abbr"] for r in conn.execute(
        "SELECT abbr FROM nfl_teams WHERE season_id=? AND bye_ff_week=?", (season_id, ff_week))}


def store_week_games(conn, season_id: int, ff_week: int, games) -> None:
    """Record the week's games as the schedule has them now: iterable of
    (game_id, home, away, kickoff datetime or None, final score posted)."""
    conn.execute("DELETE FROM nfl_week_games WHERE season_id=? AND ff_week=?",
                 (season_id, ff_week))
    conn.executemany(
        "INSERT INTO nfl_week_games(season_id,ff_week,game_id,home_team,away_team,kickoff,final) "
        "VALUES (?,?,?,?,?,?,?)",
        [(season_id, ff_week, gid, home, away, ko.isoformat() if ko else None, int(bool(fin)))
         for gid, home, away, ko, fin in games])


# --- teams --------------------------------------------------------------------

def _settled(conn, season_id: int, ff_week: int) -> bool:
    """Finalized, or scored before in-progress tracking existed and not flagged."""
    scored = conn.execute("SELECT 1 FROM team_week_scores WHERE season_id=? AND ff_week=? "
                          "AND computed_points IS NOT NULL LIMIT 1",
                          (season_id, ff_week)).fetchone()
    return is_finalized(conn, season_id, ff_week) or (
        scored is not None and ff_week not in live_weeks(conn, season_id))


def starter_states(conn, season_id: int, ff_week: int, team_id: int,
                   now: _dt.datetime | None = None) -> dict[tuple[str, str], dict]:
    """{(roster_slot, asset_ref): {"state", "kickoff"}} for one team's starters.

    state: final     — his game's stats are locked (or the week is settled);
           playing   — his game has kicked off, not over yet;
           over      — final score posted, stats not locked yet ("scoring soon");
           upcoming  — hasn't kicked off (kickoff is its ISO time, if known);
           bye       — his NFL team has no game this week;
           nogame    — he isn't on an NFL team we know of.
    Every starter who isn't final has a state other than "final", so a row the
    site leaves untagged is finished — including one that scored 0."""
    now = now or _dt.datetime.now(ET)
    settled = _settled(conn, season_id, ff_week)
    locked = locked_teams(conn, season_id, ff_week)
    byes = bye_teams(conn, season_id, ff_week)
    games: dict[str, tuple] = {}
    for g in conn.execute("SELECT home_team, away_team, kickoff, final FROM nfl_week_games "
                          "WHERE season_id=? AND ff_week=?", (season_id, ff_week)):
        ko = _dt.datetime.fromisoformat(g["kickoff"]) if g["kickoff"] else None
        for t in (g["home_team"], g["away_team"]):
            games[t] = (ko, bool(g["final"]))

    out = {}
    for r in conn.execute(
            "SELECT l.roster_slot, l.asset_ref, "
            "CASE WHEN l.asset_kind='TEAM_UNIT' THEN l.asset_ref ELSE p.nfl_team_abbr END nfl "
            "FROM weekly_lineups l "
            "LEFT JOIN nfl_players p ON p.season_id=l.season_id AND p.gsis_id=l.asset_ref "
            "WHERE l.season_id=? AND l.ff_week=? AND l.team_id=?",
            (season_id, ff_week, team_id)):
        ko = None
        if settled or r["nfl"] in locked:
            state = "final"
        elif r["nfl"] is None:
            state = "nogame"
        elif r["nfl"] in byes:
            state = "bye"
        else:
            ko, fin = games.get(r["nfl"], (None, False))
            state = "over" if fin else ("playing" if ko and now >= ko else "upcoming")
        out[(r["roster_slot"], r["asset_ref"])] = {
            "state": state, "kickoff": ko.isoformat() if ko and state == "upcoming" else None}
    return out


def statuses(conn, season_id: int, ff_week: int,
             now: _dt.datetime | None = None) -> dict[int, dict]:
    """{team_id: status} for every team with at least one starter this week.

    status: starters, has_lineup (all 9 set), to_play (starters whose game
    isn't locked yet), open (starters with no game — on bye), floor (points
    locked in), adjusted (commissioner-overridden total), done."""
    now = now or _dt.datetime.now(ET)
    settled = _settled(conn, season_id, ff_week)
    locked = locked_teams(conn, season_id, ff_week)
    byes = bye_teams(conn, season_id, ff_week)
    _, last = kickoffs(conn, season_id, ff_week)
    week_over = last is not None and now >= last
    adjusted = {r["team_id"] for r in conn.execute(
        "SELECT team_id FROM team_week_scores WHERE season_id=? AND ff_week=? AND adjusted=1",
        (season_id, ff_week))}

    out: dict[int, dict] = {}
    for r in conn.execute(
            "SELECT l.team_id, "
            "CASE WHEN l.asset_kind='TEAM_UNIT' THEN l.asset_ref ELSE p.nfl_team_abbr END nfl, "
            "COALESCE(a.points, 0) pts FROM weekly_lineups l "
            "LEFT JOIN nfl_players p ON p.season_id=l.season_id AND p.gsis_id=l.asset_ref "
            "LEFT JOIN asset_week_scores a ON a.season_id=l.season_id AND a.ff_week=l.ff_week "
            "AND a.asset_kind=l.asset_kind AND a.asset_ref=l.asset_ref "
            "AND (l.asset_kind='PLAYER' OR a.unit_type=l.roster_slot) "
            "WHERE l.season_id=? AND l.ff_week=?", (season_id, ff_week)):
        s = out.setdefault(r["team_id"], {"starters": 0, "to_play": 0, "open": 0, "floor": 0.0})
        s["starters"] += 1
        if r["nfl"] in locked:
            s["floor"] += r["pts"]
        elif r["nfl"] is None or r["nfl"] in byes:
            s["open"] += 1
        else:
            s["to_play"] += 1

    for tid, s in out.items():
        s["has_lineup"] = s["starters"] >= 9
        s["adjusted"] = tid in adjusted
        s["settled"] = settled
        if settled:
            s["to_play"], s["done"] = 0, True
        else:
            s["done"] = (s["has_lineup"] and s["to_play"] == 0
                         and (s["open"] == 0 or week_over))
    return out
