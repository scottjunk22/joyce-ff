"""
Does ESPN's box score still change after a game goes Final?

A game locks from ESPN only after it has shown Final for ESPN_SETTLE_MINUTES
(scoring.py), on the theory that the last box-score touch-ups land in those
minutes. That wait is a precaution, not something we've seen ESPN need. To find
out, the always-on checker keeps a copy of each game's stat lines at three
points: when it first sees Final, at the lock, and 30 minutes after the lock.
`manage.py settle-report` lists every stat that differs between them.

Nothing here affects scoring — it only records and compares.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict

FOLLOW_UP = _dt.timedelta(minutes=30)       # after the lock
FOLLOW_UP_WINDOW = _dt.timedelta(hours=6)    # give up after this
STAGES = ("final", "lock", "plus30")
_PLAYER_FIELDS = ("rushing_yards", "rushing_tds", "receptions", "receiving_yards",
                  "receiving_tds", "return_tds", "two_point_conversions")


def snapshot(conn, season_id, ff_week, game_id, event_id, stage, lines, now) -> None:
    """Keep this stage's copy of a game's stat lines (the first one only)."""
    conn.execute(
        "INSERT OR IGNORE INTO espn_snapshots(season_id,ff_week,game_id,event_id,stage,taken_at,lines) "
        "VALUES (?,?,?,?,?,?,?)",
        (season_id, ff_week, game_id, event_id, stage, now.isoformat(), json.dumps(asdict(lines))))


def _due(conn, season_id, now):
    """Locked-from-ESPN games whose 30-minute follow-up copy hasn't been taken:
    [(ff_week, game_id, event_id, lock time)]."""
    out = []
    for r in conn.execute(
            "SELECT l.ff_week, l.game_id, l.event_id, l.taken_at FROM espn_snapshots l "
            "LEFT JOIN espn_snapshots p ON p.season_id=l.season_id AND p.game_id=l.game_id "
            "AND p.stage='plus30' WHERE l.season_id=? AND l.stage='lock' AND p.id IS NULL",
            (season_id,)):
        locked = _dt.datetime.fromisoformat(r["taken_at"])
        if now - locked <= FOLLOW_UP_WINDOW:
            out.append((r["ff_week"], r["game_id"], r["event_id"], locked))
    return out


def follow_up_pending(conn, season_id, now) -> bool:
    """Keeps the always-on checker awake until every follow-up copy is taken."""
    return bool(_due(conn, season_id, now))


def run_follow_ups(conn, season_id, now) -> int:
    """Take any follow-up copies that are due. Returns how many."""
    from ..data_sources import espn

    n = 0
    for ff_week, game_id, event_id, locked in _due(conn, season_id, now):
        if now - locked >= FOLLOW_UP:
            snapshot(conn, season_id, ff_week, game_id, event_id, "plus30",
                     espn.game_lines(event_id), now)
            n += 1
    conn.commit()
    return n


def differences(a: dict, b: dict) -> list[str]:
    """Every stat that differs between two copies of a game's lines."""
    out = []
    for pid in sorted(set(a["players"]) | set(b["players"])):
        pa, pb = a["players"].get(pid, {}), b["players"].get(pid, {})
        name = pb.get("name") or pa.get("name") or pid
        for f in _PLAYER_FIELDS:
            if pa.get(f, 0) != pb.get(f, 0):
                out.append(f"{name}: {f} {pa.get(f, 0):g} -> {pb.get(f, 0):g}")
    for team in sorted(set(a["units"]) | set(b["units"])):
        ua, ub = a["units"].get(team, {}), b["units"].get(team, {})
        for k in sorted(set(ua) | set(ub)):
            if ua.get(k) != ub.get(k):
                out.append(f"{team} {k}: {ua.get(k)} -> {ub.get(k)}")
    if a.get("unknown_scoring") != b.get("unknown_scoring"):
        out.append(f"unrecognised scoring plays: {a.get('unknown_scoring')} -> {b.get('unknown_scoring')}")
    return out


def report(conn, season_id) -> list[str]:
    """One line per game per comparison, plus each difference found."""
    games: dict[str, dict] = {}
    for r in conn.execute("SELECT game_id, stage, taken_at, lines FROM espn_snapshots "
                          "WHERE season_id=? ORDER BY game_id", (season_id,)):
        games.setdefault(r["game_id"], {})[r["stage"]] = (
            _dt.datetime.fromisoformat(r["taken_at"]), json.loads(r["lines"]))
    lines = []
    for gid, st in games.items():
        for x, y in (("final", "lock"), ("lock", "plus30")):
            if x in st and y in st:
                mins = (st[y][0] - st[x][0]).total_seconds() / 60
                diffs = differences(st[x][1], st[y][1])
                lines.append(f"{gid}  {x} -> {y} ({mins:.0f} min): "
                             + ("no changes" if not diffs else f"{len(diffs)} CHANGED"))
                lines += [f"      {d}" for d in diffs]
            elif x in st:
                lines.append(f"{gid}  {x} -> {y}: waiting for the {y} copy")
    return lines
