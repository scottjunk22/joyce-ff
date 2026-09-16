"""
The commissioner's double-check on weekly scores (commissioner, 2026-09-15).

ESPN locks a game's points minutes after the final whistle; nflverse publishes
the same game's official stats a day or so later, and the runner compares the
two (scoring.ingest_asset_scores_from_nflverse). A difference already goes to
the commissioner as a stat check. This module reports the rest: how far the
checking has got, week by week, so a quiet commissioner tab means "checked and
matched" rather than "not looked at yet".

A game is CHECKED once nflverse has been compared against it (or it locked from
nflverse in the first place). A game nflverse still hasn't published when the
comparison window closes can't be checked; its week stays listed until the
commissioner dismisses it, so an unchecked game never slips away quietly.

The commissioner tab lists at most SHOW_WEEKS weeks: every unfinished week, then
finished weeks newest first in whatever room is left. The rest are "earlier".
"""

from __future__ import annotations

import datetime as _dt

from . import display, progress
from .progress import ET

SHOW_WEEKS = 3


def _dismiss_key(season_id: int, ff_week: int) -> str:
    return f"score_check_dismissed:{season_id}:{ff_week}"


def dismiss_week(conn, season_id: int, ff_week: int, by: str | None) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                 (_dismiss_key(season_id, ff_week), by or "commissioner"))
    conn.commit()


def _when(iso: str | None) -> str | None:
    if not iso:
        return None
    t = _dt.datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return progress._ct_label(t)


def _week(conn, season_id: int, ff_week: int, now: _dt.datetime) -> dict | None:
    from .runner import CROSS_CHECK_AFTER_FINAL

    games = conn.execute("SELECT game_id, home_team, away_team, kickoff FROM nfl_week_games "
                         "WHERE season_id=? AND ff_week=? ORDER BY kickoff, game_id",
                         (season_id, ff_week)).fetchall()
    first, last = progress.kickoffs(conn, season_id, ff_week)
    if not games or first is None or now < first:
        return None
    locks = {r["game_id"]: r for r in conn.execute(
        "SELECT game_id, source, locked_at, verified_at FROM nfl_game_locks "
        "WHERE season_id=? AND ff_week=?", (season_id, ff_week))}
    closed = (progress.is_finalized(conn, season_id, ff_week)
              and last is not None and now > last + CROSS_CHECK_AFTER_FINAL)
    # Stat checks by NFL team, so a game can say it had one.
    diffs: dict[str, list] = {}
    for r in conn.execute(
            "SELECT s.resolution, CASE WHEN s.asset_kind='PLAYER' THEN p.nfl_team_abbr "
            "ELSE s.asset_ref END team FROM stat_checks s "
            "LEFT JOIN nfl_players p ON p.season_id=s.season_id AND p.gsis_id=s.asset_ref "
            "WHERE s.season_id=? AND s.ff_week=?", (season_id, ff_week)):
        diffs.setdefault(r["team"], []).append(r["resolution"])

    out = []
    for g in games:
        label = f"{display.team(g['away_team'])} @ {display.team(g['home_team'])}"
        lock = locks.get(g["game_id"])
        seen = diffs.get(g["home_team"], []) + diffs.get(g["away_team"], [])
        if lock and (lock["source"] == "nflverse" or lock["verified_at"]):
            open_n = sum(1 for x in seen if x is None)
            if open_n:
                status, text = "diff", f"checked · {open_n} difference{'s' if open_n > 1 else ''}, see Needs your attention"
            elif seen:
                status, text = "checked", "checked · difference settled"
            else:
                status, text = "checked", f"checked {_when(lock['verified_at'] or lock['locked_at'])}"
        elif closed:
            status, text = "unchecked", "couldn't be checked — nflverse never posted it"
        else:
            ko = _dt.datetime.fromisoformat(g["kickoff"]) if g["kickoff"] else None
            if lock or (ko and now >= ko):
                status, text = "waiting", "waiting on nflverse"
            else:
                status, text = "upcoming", f"kicks off {progress._ct_label(ko)}" if ko else "not played yet"
        out.append({"game": label, "status": status, "text": text})

    checked = sum(1 for g in out if g["status"] in ("checked", "diff"))
    unchecked = [g["game"] for g in out if g["status"] == "unchecked"]
    dismissed = bool(conn.execute("SELECT 1 FROM settings WHERE key=?",
                                  (_dismiss_key(season_id, ff_week),)).fetchone())
    done = all(g["status"] in ("checked", "diff", "unchecked") for g in out)
    order = {"diff": 0, "waiting": 1, "upcoming": 2, "unchecked": 3, "checked": 4}
    return {"week": ff_week, "total": len(out), "checked": checked,
            "differences": sum(1 for g in out if g["status"] == "diff"),
            "unchecked": unchecked, "dismissed": dismissed,
            "can_dismiss": bool(unchecked) and done and not dismissed,
            "finished": done and (not unchecked or dismissed),
            "games": sorted(out, key=lambda g: order[g["status"]])}


def weeks(conn, season_id: int, now: _dt.datetime | None = None) -> dict:
    """{"recent": [...up to SHOW_WEEKS weeks...], "earlier": [...]}, newest first."""
    now = now or _dt.datetime.now(ET)
    all_weeks = [w for w in (
        _week(conn, season_id, r["ff_week"], now) for r in conn.execute(
            "SELECT DISTINCT ff_week FROM nfl_week_games WHERE season_id=? ORDER BY ff_week DESC",
            (season_id,)))
        if w]
    keep = {w["week"] for w in all_weeks if not w["finished"]}
    for w in all_weeks:
        if len(keep) >= SHOW_WEEKS:
            break
        keep.add(w["week"])
    return {"recent": [w for w in all_weeks if w["week"] in keep],
            "earlier": [w for w in all_weeks if w["week"] not in keep]}


def resolve_stat_check(conn, season_id: int, check_id: int, action: str, by: str | None) -> dict:
    """The commissioner's call on a stat check: keep the posted points, or change
    them to the other source's. A change rewrites that line (with a note in its
    breakdown) and re-totals the week; records follow from the new totals."""
    import json

    from . import scoring

    row = conn.execute("SELECT * FROM stat_checks WHERE id=? AND season_id=?",
                       (check_id, season_id)).fetchone()
    if not row:
        raise ValueError("no such stat check")
    if row["resolution"]:
        raise ValueError("that stat check was already settled")
    if action not in ("keep", "change"):
        raise ValueError("choose keep or change")
    if action == "change":
        line = conn.execute(
            "SELECT breakdown_json FROM asset_week_scores WHERE season_id=? AND ff_week=? "
            "AND asset_kind=? AND asset_ref=? AND unit_type=?",
            (season_id, row["ff_week"], row["asset_kind"], row["asset_ref"], row["unit_type"])).fetchone()
        # Take the other source's own lines, so the box score explains the new
        # number instead of showing the old lines plus a correction.
        if row["other_breakdown"]:
            items = json.loads(row["other_breakdown"])
        else:
            items = json.loads(line["breakdown_json"] or "[]") if line else []
            items.append([f"Stat correction ({row['source']})",
                          row["other_points"] - row["locked_points"]])
        items.append([f"corrected from {row['source']} · was {row['locked_points']:g}", 0])
        conn.execute(
            "UPDATE asset_week_scores SET points=?, breakdown_json=? WHERE season_id=? AND ff_week=? "
            "AND asset_kind=? AND asset_ref=? AND unit_type=?",
            (row["other_points"], json.dumps(items), season_id, row["ff_week"],
             row["asset_kind"], row["asset_ref"], row["unit_type"]))
    conn.execute("UPDATE stat_checks SET resolution=?, resolved_by=?, resolved_at=? WHERE id=?",
                 ("changed" if action == "change" else "kept", by, scoring._now(), check_id))
    conn.commit()
    if action == "change":
        scoring.score_team_week(conn, season_id, row["ff_week"])
    return {"week": row["ff_week"], "points": row["other_points"] if action == "change" else row["locked_points"]}
