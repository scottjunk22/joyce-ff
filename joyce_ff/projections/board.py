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
from ..scoring import rules
from . import history
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


def _player_records(board) -> list[dict]:
    recs = []
    for slot in ("RB", "R"):
        sub = board[board["slot"] == slot].reset_index(drop=True)
        for i, r in sub.iterrows():
            recs.append({
                "rank": int(i) + 1,
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


def _unit_records(unit_boards) -> dict:
    out = {}
    for unit, df in unit_boards.items():
        rows = []
        for i, r in df.reset_index(drop=True).iterrows():
            rows.append({
                "rank": int(i) + 1,
                "team": str(r["team"]),
                "proj": _num(r.get("proj")),
                "vor": _num(r.get("vor")),
                "floor": _num(r.get("p25")),
                "ceil": _num(r.get("p75")),
                "games25": int(r["games_2025"]) if _num(r.get("games_2025")) is not None else 0,
            })
        out[unit] = rows
    return out


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

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seasons": list(seasons),
        "draft_season": draft_season,
        "league": {
            "teams": rules.NUM_TEAMS,
            "division_teams": rules.DIVISION_TEAMS,
            "rostered_rb": rules.ROSTERED_RB_DIVISION,
            "rostered_r": rules.ROSTERED_R_DIVISION,
            "started_rb": rules.STARTED_RB_DIVISION,
            "started_r": rules.STARTED_R_DIVISION,
        },
        "replacement": replacement,
        "players": _player_records(pboard),
        "units": _unit_records(uboard),
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
