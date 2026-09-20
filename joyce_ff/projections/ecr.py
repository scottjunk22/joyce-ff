"""
FantasyPros consensus rankings (ECR), imported from a CSV a logged-in user
downloads — never scraped (their terms forbid it, and their projections are
PPR/standard, which is numerically meaningless under threshold scoring).

So we take the ORDER and nothing else. A consensus rank is a hundred analysts'
read on roles, health and rookies — exactly the things three seasons of history
can't see — while our projection is the only thing that knows what a point is
worth in this league. Shown side by side, the disagreements are the signal.

Their positions map onto our slots, which are not theirs:

    RB              -> RB
    WR, TE          -> R          (our R is WR + TE, no limits)
    QB  (a player)  -> that team's QB unit
    K   (a player)  -> that team's K unit
    DST (a team)    -> that team's DEF/ST unit

A team unit takes its team's BEST-ranked player of that kind — the QB room is
ranked by the quarterback most likely to play it. Nothing maps to the coach
slot; those cells stay blank, as they should.

Names are matched conservatively, the same rule as titles.py: squash case,
punctuation and suffixes, and when a name is ambiguous or absent, REPORT IT
rather than guess. An unmatched row is a visible number in the UI, not a
silently dropped player.
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ECR_CACHE = Path(__file__).resolve().parents[2] / "data" / "ecr.json"

# FantasyPros position family -> our slot. QB/K/DST become team units.
PLAYER_SLOT = {"RB": "RB", "WR": "R", "TE": "R"}
UNIT_SLOT = {"QB": "QB", "K": "K", "DST": "DEF/ST"}

# They spell two clubs differently from the NFL schedule, which is what we
# store: the Rams are "LA" here and "LAR" there, Jacksonville "JAX" vs "JAC".
TEAM_ALIAS = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS"}

# Players FantasyPros lists under a different name from the NFL's own rosters —
# a nickname, a short form, a spelling. Hand-added only, after checking the
# roster, exactly like titles.ALIASES: a wrong match here silently hands one
# player's ranking to another. Left side is their spelling, right side ours.
NAME_ALIAS = {
    "hollywood brown": "marquise brown",      # PHI WR
    "joshua palmer": "josh palmer",           # BUF WR
    "matt hibner": "matthew hibner",          # BAL TE
}

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")
_POS = re.compile(r"^([A-Za-z/]+?)(\d+)$")


def norm_name(name: str | None) -> str:
    """The comparison form of a player's name. Not for display."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    s = s.lower().replace(".", " ").replace("'", "").replace("-", " ")
    s = _SUFFIX.sub(" ", s)
    s = " ".join(s.split())
    return NAME_ALIAS.get(s, s)


def team_code(abbr: str | None) -> str | None:
    """Their team spelling in ours. 'FA' (free agent) has no team."""
    if not abbr:
        return None
    a = str(abbr).strip().upper()
    if a in ("FA", "-", ""):
        return None
    return TEAM_ALIAS.get(a, a)


class EcrError(Exception):
    """The file isn't the export we expect. Never parsed on a guess."""


def parse_csv(path: str | Path) -> list[dict]:
    """Rows of {rank, name, team, pos, pos_rank} from a FantasyPros export.

    Their exports carry blank spacer rows and quote every field; both are fine.
    A missing column is fatal — a rankings file we can't read is an error, not
    an empty board column.
    """
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            header = [h.strip().strip('"').upper() for h in next(reader)]
        except StopIteration:
            raise EcrError("the rankings file is empty") from None
        want = {"RK": None, "PLAYER NAME": None, "TEAM": None, "POS": None}
        for key in want:
            if key not in header:
                raise EcrError(f"the rankings file has no {key!r} column "
                               f"(found: {', '.join(header)})")
            want[key] = header.index(key)
        rows = []
        for raw in reader:
            if len(raw) <= max(i for i in want.values()):
                continue                                   # spacer row
            rank, name = raw[want["RK"]].strip(), raw[want["PLAYER NAME"]].strip()
            pos = raw[want["POS"]].strip().upper()
            if not rank.isdigit() or not name or not pos:
                continue
            m = _POS.match(pos)
            rows.append({"rank": int(rank), "name": name,
                         "team": team_code(raw[want["TEAM"]]),
                         "pos": (m.group(1) if m else pos).upper(),
                         "pos_rank": int(m.group(2)) if m else None})
    if not rows:
        raise EcrError("no ranking rows found in that file")
    return rows


def save(rows: list[dict], source: str, path: str | Path = ECR_CACHE) -> dict:
    data = {"source": source, "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rows": rows}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return data


def load(path: str | Path = ECR_CACHE) -> dict | None:
    """The imported rankings, or None when none have been imported. Absent is
    a normal state: the board simply has no ECR column."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def match(rows: list[dict], pool: list[dict]) -> dict:
    """Attach ranks to our assets.

    pool: [{"player_id", "name", "team"}] — the draftable players.
    Returns {"players": {player_id: {...}}, "units": {(slot, team): {...}},
             "unmatched": [names], "matched": n, "total": n}.
    """
    index: dict[str, list[dict]] = {}
    for p in pool:
        index.setdefault(norm_name(p.get("name")), []).append(p)

    players: dict[str, dict] = {}
    units: dict[tuple, dict] = {}
    unmatched: list[str] = []
    considered = 0
    for r in rows:
        entry = {"ecr": r["rank"], "ecr_pos": r["pos_rank"], "ecr_pos_kind": r["pos"]}
        if r["pos"] in UNIT_SLOT:
            considered += 1
            if not r["team"]:
                unmatched.append(r["name"])
                continue
            key = (UNIT_SLOT[r["pos"]], r["team"])
            if key not in units or r["rank"] < units[key]["ecr"]:
                units[key] = entry                          # the team's best
            continue
        if r["pos"] not in PLAYER_SLOT:
            continue                                        # a kind we don't roster
        considered += 1
        cands = index.get(norm_name(r["name"]), [])
        if len(cands) > 1 and r["team"]:
            cands = [c for c in cands if c.get("team") == r["team"]] or cands
        if len(cands) != 1:
            unmatched.append(r["name"])                     # absent or ambiguous
            continue
        pid = str(cands[0]["player_id"])
        if pid not in players or r["rank"] < players[pid]["ecr"]:
            players[pid] = entry
    return {"players": players, "units": units, "unmatched": unmatched,
            "matched": considered - len(unmatched), "total": considered}
