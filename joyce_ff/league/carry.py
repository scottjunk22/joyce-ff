"""
Carrying a lineup forward for a manager who didn't set one.

The commissioner's rule: an unset lineup carries forward the previous week's.
The copy is made when the week's FIRST game kicks off — the moment the rule
starts to matter — and from then on it is the team's lineup exactly as if the
manager had submitted it. They can still change anyone whose game hasn't
started; anyone whose game has started is locked in, including a Thursday-night
player from last week's lineup.

A straight copy would carry last week's problems with it, so it's made the way
the commissioner would make it by hand:
  1. a player since traded away is replaced by the player who came in for him;
  2. last week's Open rental (a one-week player) gives way to the player he
     covered, now back from his bye;
  3. a bye-week flex lineup (3 RB or 4 R) that isn't legal this week goes back
     to the lineup from before the bye week — the extra player sits, the ones
     who were on bye return;
  4. this week's Opens start in place of the players they cover;
  5. a starter on bye is replaced from the bench at the same position, and when
     the bench runs out, by the bye-week flex (2+ uncovered players on bye).

Anything it can't settle — a choice it would have to guess at, a team unit on
bye with no Open — is left as copied and written into carry_note so the
commissioner sees it. It never guesses.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..scoring import rules
from . import repo

SKILL = ("RB", "R")
# A flex composition -> the position whose byes justified it.
_FLEX_SHORT = {(3, 2): "R", (1, 4): "RB"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _composition(lineup) -> tuple[int, int]:
    slots = [l["slot"] for l in lineup]
    return slots.count("RB"), slots.count("R")


def _lineup(conn, sid, tid, wk) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT roster_slot, asset_kind, asset_ref, unit_type, is_rental FROM weekly_lineups "
        "WHERE season_id=? AND team_id=? AND ff_week=? ORDER BY id", (sid, tid, wk))]


def _opens(conn, sid, tid, wk) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT position, out_asset_ref out_ref, in_asset_ref in_ref FROM transactions "
        "WHERE season_id=? AND team_id=? AND ff_week=? AND type='OPEN' AND reversed=0 "
        "ORDER BY id", (sid, tid, wk))]


def _trades_since(conn, sid, tid, wk) -> dict[tuple[str, str], str]:
    """(position, player traded away) -> the player who came in, for trades
    made in week `wk` or later."""
    return {(r["position"], r["out_asset_ref"]): r["in_asset_ref"] for r in conn.execute(
        "SELECT position, out_asset_ref, in_asset_ref FROM transactions WHERE season_id=? "
        "AND team_id=? AND ff_week>=? AND type='TRADE' AND reversed=0 ORDER BY id",
        (sid, tid, wk))}


def _last_standard_week(conn, sid, tid, before) -> int | None:
    """The most recent week before `before` whose lineup was the standard
    2 RB + 3 R — the lineup from before a bye-week flex."""
    for r in conn.execute(
            "SELECT ff_week w, SUM(roster_slot='RB') rb, SUM(roster_slot='R') r "
            "FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week<? "
            "GROUP BY ff_week ORDER BY ff_week DESC", (sid, tid, before)):
        if (r["rb"], r["r"]) == (2, 3):
            return r["w"]
    return None


class _Team:
    """One team's situation in the week being filled."""

    def __init__(self, conn, sid, tid, wk):
        self.conn, self.sid, self.tid, self.wk = conn, sid, tid, wk
        self.roster = repo.current_roster(conn, tid)
        self.owned = {(e["roster_slot"], e["asset_ref"]): e for e in self.roster}
        self.opens = _opens(conn, sid, tid, wk)

    def on_bye(self, slot, ref) -> bool:
        e = self.owned.get((slot, ref))
        kind = e["asset_kind"] if e else repo._kind_for(slot)
        return repo.is_on_bye(self.conn, self.sid, kind, ref, self.wk)

    def stuck(self, l) -> bool:
        return l["ref"] is None or (not l["rental"] and self.on_bye(l["slot"], l["ref"]))

    def bench(self, slot, taken) -> list[str]:
        """Players at `slot` who aren't starting and have a game this week."""
        return [e["asset_ref"] for e in self.roster if e["roster_slot"] == slot
                and e["asset_ref"] not in taken and not self.on_bye(slot, e["asset_ref"])]

    def uncovered_byes(self, slot) -> int:
        return repo._bye_count(self.conn, self.sid, self.tid, slot, self.wk)

    def last_started(self, ref) -> int:
        r = self.conn.execute(
            "SELECT MAX(ff_week) w FROM weekly_lineups WHERE season_id=? AND team_id=? "
            "AND asset_ref=? AND ff_week<?", (self.sid, self.tid, ref, self.wk)).fetchone()
        return r["w"] if r and r["w"] is not None else 0

    def label(self, slot, ref) -> str:
        if slot in SKILL:
            r = self.conn.execute("SELECT name FROM nfl_players WHERE season_id=? AND gsis_id=?",
                                  (self.sid, ref)).fetchone()
            return r["name"] if r else str(ref)
        return f"{ref} {slot}"

    def name(self, l) -> str:
        return self.label(l["slot"], l["ref"]) if l["ref"] else f"the empty {l['slot']} slot"


def _traced(team: _Team, week: int, notes: list[str]) -> list[dict]:
    """That week's lineup, each starter traced to who fills his spot now:
    a rental back to the player he covered, a traded player to his replacement."""
    opened = {o["in_ref"]: o["out_ref"] for o in _opens(team.conn, team.sid, team.tid, week)}
    trades = _trades_since(team.conn, team.sid, team.tid, week)
    out = []
    for r in _lineup(team.conn, team.sid, team.tid, week):
        slot, ref = r["roster_slot"], r["asset_ref"]
        if r["is_rental"]:
            ref = opened.get(ref)
        if ref is not None:
            seen = set()
            while (slot, ref) not in team.owned and (slot, ref) in trades and ref not in seen:
                seen.add(ref)
                ref = trades[(slot, ref)]
            if team.owned and (slot, ref) not in team.owned:
                notes.append(f"{team.label(slot, ref)} is no longer on the roster and no "
                             "trade shows who replaced him")
        out.append({"slot": slot, "ref": ref, "rental": False,
                    "kind": r["asset_kind"], "unit": r["unit_type"]})
    return out


def _pick(team: _Team, cands) -> str | None:
    """One replacement: the only candidate, or the one the manager started most
    recently. None when that still doesn't settle it."""
    cands = list(dict.fromkeys(cands))
    if len(cands) <= 1:
        return cands[0] if cands else None
    ranked = sorted(cands, key=team.last_started, reverse=True)
    if team.last_started(ranked[0]) > team.last_started(ranked[1]):
        return ranked[0]
    return None


def carry_team(conn, sid: int, tid: int, wk: int) -> bool:
    """Give one team last week's lineup for week `wk` if it has none. Returns
    True if a lineup was written."""
    if conn.execute("SELECT 1 FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week=? "
                    "LIMIT 1", (sid, tid, wk)).fetchone():
        return False
    prev = conn.execute("SELECT MAX(ff_week) w FROM weekly_lineups WHERE season_id=? "
                        "AND team_id=? AND ff_week<?", (sid, tid, wk)).fetchone()["w"]
    if prev is None:
        return False

    team = _Team(conn, sid, tid, wk)
    need = rules.BYE_FLEX_MIN_ON_BYE
    notes: list[str] = []

    # 1-2. Last week's lineup, traced to who's on the roster now.
    lineup = _traced(team, prev, notes)

    # 3. A bye-week flex that isn't legal this week goes back to the lineup
    #    from before the bye week.
    short = _FLEX_SHORT.get(_composition([l for l in lineup if l["slot"] in SKILL]))
    if short and team.uncovered_byes(short) < need:
        before = _last_standard_week(conn, sid, tid, prev)
        if before is None:
            notes.append("last week's lineup used the bye-week flex, which isn't allowed "
                         "this week, and there's no earlier lineup to go back to")
        else:
            lineup = ([l for l in lineup if l["slot"] not in SKILL]
                      + [l for l in _traced(team, before, notes) if l["slot"] in SKILL])

    # 4. This week's Opens start in place of the players they cover.
    spare = []
    for o in team.opens:
        hit = next((l for l in lineup
                    if l["slot"] == o["position"] and l["ref"] == o["out_ref"]), None)
        if hit:
            hit.update(ref=o["in_ref"], rental=True)
        else:
            spare.append(o)

    # 5. A starter on bye, or a slot that couldn't be traced, is replaced at the
    #    same position — an unused Open rental first, then the bench ...
    for l in [l for l in lineup if team.stuck(l)]:
        taken = {x["ref"] for x in lineup if x["ref"]}
        rentals = [o["in_ref"] for o in spare
                   if o["position"] == l["slot"] and o["in_ref"] not in taken]
        pool, rental = (rentals, True) if rentals else (team.bench(l["slot"], taken), False)
        pick = _pick(team, pool)
        if pick:
            l.update(ref=pick, rental=rental)
        elif pool:
            l["ambiguous"] = True
            notes.append(f"couldn't tell who should replace {team.name(l)} at {l['slot']} "
                         "— the commissioner should choose")
    #    ... and when the bench runs out, by the bye-week flex: the extra RB for
    #    a receiver (or receiver for an RB) when 2+ uncovered players at that
    #    position are on bye. One flex at most.
    for l in [l for l in lineup if team.stuck(l) and l["slot"] in SKILL
              and not l.get("ambiguous")]:
        if (_composition([x for x in lineup if x["slot"] in SKILL]) != (2, 3)
                or team.uncovered_byes(l["slot"]) < need):
            continue
        other = "RB" if l["slot"] == "R" else "R"
        pick = _pick(team, team.bench(other, {x["ref"] for x in lineup if x["ref"]}))
        if pick:
            l.update(slot=other, ref=pick, rental=False, kind=None, unit=None)

    for l in lineup:
        if l["ref"] is None:
            notes.append(f"no one was available for a {l['slot']} slot")
        elif team.stuck(l) and not l.get("ambiguous"):
            notes.append(f"{team.name(l)} is on bye with no one to replace him "
                         "— an Open would cover him")

    full = [l for l in lineup if l["ref"]]
    if len(full) == 9:
        rule = repo.LEGAL_SKILL.get(_composition([l for l in full if l["slot"] in SKILL]),
                                    "ILLEGAL")
        if (rule == "ILLEGAL"
                or (rule == "recv_bye" and team.uncovered_byes("R") < need)
                or (rule == "rb_bye" and team.uncovered_byes("RB") < need)):
            notes.append("the carried lineup isn't a legal combination this week")

    note = "; ".join(dict.fromkeys(notes)) or None
    now = _now()
    for l in full:
        e = team.owned.get((l["slot"], l["ref"]))
        if e and not l["rental"]:
            kind, unit = e["asset_kind"], e["unit_type"]
        elif l["rental"] or not l.get("kind"):
            kind = repo._kind_for(l["slot"])
            unit = l["slot"] if kind == "TEAM_UNIT" else None
        else:
            kind, unit = l["kind"], l["unit"]
        conn.execute(
            "INSERT OR IGNORE INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,"
            "asset_kind,asset_ref,unit_type,is_rental,submitted_at,carried_from,carry_note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, tid, wk, l["slot"], kind, l["ref"], unit, int(l["rental"]), now, prev, note))
    return True


def carry_forward(conn, season_id: int, ff_week: int) -> int:
    """Carry a lineup forward for every team without one this week. Returns how
    many teams got one."""
    n = 0
    for r in conn.execute("SELECT id FROM teams WHERE season_id=? ORDER BY id",
                          (season_id,)).fetchall():
        n += carry_team(conn, season_id, r["id"], ff_week)
    conn.commit()
    return n
