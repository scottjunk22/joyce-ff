"""
Assemble every draft board into one JSON-serializable payload for the UI,
and cache it to disk so the draft tool starts instantly and works offline.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from ..data_sources import nflverse as nv
from ..draft import order as draft_order
from ..scoring import rules
from . import history
from . import sos
from . import valuation as val

BOARD_CACHE = Path(__file__).resolve().parents[2] / "data" / "boards.json"
DRAFT_SEASON = 2026


def _num(x):
    """JSON-safe number: NaN/inf -> None, numpy -> float."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, 2)


def current_form(season: int) -> tuple[dict, dict, int]:
    """Per-asset points/game for the season IN PROGRESS — display only, never
    folded into the projection. Two weeks is far too small a sample to move a
    three-season average honestly, but it's exactly what shows a changed role,
    so it earns its own column rather than a thumb on the scale.

    Returns ({player_id: (ppg, games)}, {(unit, team): (ppg, games)}, weeks).
    Empty when the season hasn't started or nflverse hasn't published it yet —
    a missing source leaves blank cells, never invented numbers.
    """
    players, units, weeks = {}, {}, 0
    try:
        sp = history.scored_player_games([season])
    except Exception:
        return players, units, weeks           # not published yet
    if sp is not None and not sp.empty:
        weeks = int(sp["week"].max())
        for pid, grp in sp.groupby("player_id"):
            players[str(pid)] = (float(grp["points"].mean()), int(len(grp)))
    try:
        ub = history.scored_team_unit_games([season])
    except Exception:
        return players, units, weeks
    for unit, df in (ub or {}).items():
        if df is None or df.empty:
            continue
        for team, grp in df.groupby("team"):
            units[(unit, str(team))] = (float(grp["points"].mean()), int(len(grp)))
    return players, units, weeks


def _player_records(board, form=None, sos_map=None) -> list[dict]:
    form = form or {}
    sos_map = sos_map or {}
    recs = []
    for slot in ("RB", "R"):
        sub = board[board["slot"] == slot].reset_index(drop=True)
        team_sos = sos_map.get(slot, {})
        for i, r in sub.iterrows():
            cur = form.get(str(r["player_id"]))
            tm = None if r.get("team") is None else str(r["team"])
            recs.append({
                "rank": int(i) + 1,
                "id": str(r["player_id"]),
                "cur_ppg": None if not cur else round(cur[0], 2),
                "cur_g": None if not cur else cur[1],
                "sos": team_sos.get(tm),
                "slot": slot,
                "tier": None if _num(r.get("tier")) is None else int(r["tier"]),
                "name": str(r["name"]),
                "team": None if r.get("team") is None else str(r["team"]),
                "position": str(r["position"]),
                "proj": _num(r.get("proj")),
                "vor": _num(r.get("vor")),
                "floor": _num(r.get("p25")),
                "ceil": _num(r.get("p75")),
                "bust": (None if _num(r.get("bust_rate")) is None
                         else round(r["bust_rate"] * 100)),
                "games25": int(r["games_2025"]) if _num(r.get("games_2025")) is not None else 0,
                "low_sample": bool(r.get("low_sample")),
                "no_history": bool(r.get("no_history")),
            })
    return recs


def _unit_records(unit_boards, form=None) -> dict:
    form = form or {}
    out = {}
    for unit, df in unit_boards.items():
        rows = []
        for i, r in df.reset_index(drop=True).iterrows():
            cur = form.get((unit, str(r["team"])))
            rows.append({
                "rank": int(i) + 1,
                "team": str(r["team"]),
                "cur_ppg": None if not cur else round(cur[0], 2),
                "cur_g": None if not cur else cur[1],
                "proj": _num(r.get("proj")),
                "vor": _num(r.get("vor")),
                "floor": _num(r.get("p25")),
                "ceil": _num(r.get("p75")),
                "bust": (None if _num(r.get("bust_rate")) is None
                         else round(r["bust_rate"] * 100)),
                "games25": int(r["games_2025"]) if _num(r.get("games_2025")) is not None else 0,
            })
        out[unit] = rows
    return out


def _overall_records(player_recs: list[dict], unit_recs: dict,
                     min_gap: float = val.TIER_GAP) -> list[dict]:
    """Merge individuals + team units into one VOR-sorted draft order, with
    tiers recomputed by absolute VOR gaps across the whole merged list."""
    rows = []
    for p in player_recs:
        if p["vor"] is None:
            continue
        rows.append({k: p[k] for k in
                     ("id", "slot", "name", "team", "position", "proj", "vor",
                      "floor", "ceil", "bust", "games25", "low_sample",
                      "no_history", "cur_ppg", "cur_g", "sos")})
    for unit, lst in unit_recs.items():
        for u in lst:
            if u["vor"] is None:
                continue
            rows.append({"id": None, "slot": unit, "name": f"{u['team']} {unit}",
                         "team": u["team"], "position": unit,
                         "proj": u["proj"], "vor": u["vor"],
                         "floor": u["floor"], "ceil": u["ceil"],
                         "bust": u.get("bust"),
                         "cur_ppg": u.get("cur_ppg"), "cur_g": u.get("cur_g"),
                         "sos": None,
                         "games25": u["games25"], "low_sample": False,
                         "no_history": False})

    rows.sort(key=lambda r: r["vor"], reverse=True)
    vals = [r["vor"] for r in rows]
    tier = 1
    for i, r in enumerate(rows):
        if i > 0 and (vals[i - 1] - vals[i]) >= min_gap:
            tier += 1
        r["rank"] = i + 1
        r["tier"] = tier
    return rows


def build_all(seasons=history.SEASONS_DEFAULT, draft_season=DRAFT_SEASON) -> dict:
    sp = history.scored_player_games(seasons)
    roster = nv.load_roster(draft_season)
    pboard = val.player_board(sp, roster)

    units = history.scored_team_unit_games(seasons)
    uboard = val.team_unit_board(units)

    replacement = {}
    for slot in ("RB", "R"):
        rl = pboard[(pboard["slot"] == slot) & pboard["repl_level"].notna()]
        replacement[slot] = _num(rl["repl_level"].iloc[0]) if not rl.empty else None
    for unit, df in uboard.items():
        replacement[unit] = _num(df["repl_level"].iloc[0]) if not df.empty else None

    # Form in the season being drafted (2026 wks 1-2 by draft day). Shown as its
    # own columns; the projection deliberately stays a 2023-25 measure.
    pform, uform, cur_weeks = current_form(draft_season)
    # Strength of schedule for the slots where a matchup debate actually happens
    # (RB / R). Measured in our scoring, over the NFL weeks our season covers.
    try:
        sos_data = sos.build(sp, draft_season)
    except Exception:
        sos_data = {"ratings": {}, "sos": {}, "basis": {}, "weeks": [], "seasons": []}
    precs = _player_records(pboard, pform, sos_data.get("sos"))
    urecs = _unit_records(uboard, uform)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seasons": list(seasons),
        "draft_season": draft_season,
        "current_season": {"year": draft_season, "weeks": cur_weeks,
                           "available": bool(pform or uform)},
        "sos": {"available": bool(sos_data.get("sos")),
                "nfl_weeks": sos_data.get("weeks", []),
                "seasons": sos_data.get("seasons", []),
                "basis": sos_data.get("basis", {}),
                "by_team": sos_data.get("sos", {}),
                "defense_ratings": sos_data.get("ratings", {})},
        "league": {
            "teams": rules.NUM_TEAMS,
            "division_teams": rules.DIVISION_TEAMS,
            "rostered_rb": rules.ROSTERED_RB_DIVISION,
            "rostered_r": rules.ROSTERED_R_DIVISION,
            "started_rb": rules.STARTED_RB_DIVISION,
            "started_r": rules.STARTED_R_DIVISION,
        },
        "replacement": replacement,
        "players": precs,
        "units": urecs,
        "overall": _overall_records(precs, urecs),
        "draft": {
            "slots": draft_order.SLOTS,
            "rounds": draft_order.ROUNDS,
            "order": draft_order.DRAFT_ORDER,
            "sequence": draft_order.build_sequence(),
            "roster_template": draft_order.ROSTER_TEMPLATE,
        },
    }


def build_and_cache(**kw) -> dict:
    data = build_all(**kw)
    BOARD_CACHE.parent.mkdir(parents=True, exist_ok=True)
    BOARD_CACHE.write_text(json.dumps(data), encoding="utf-8")
    return data


def load_cached() -> dict | None:
    if BOARD_CACHE.exists():
        return json.loads(BOARD_CACHE.read_text(encoding="utf-8"))
    return None
