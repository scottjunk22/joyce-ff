"""
Repository layer: the operations on the league DB that ENFORCE the rules.

Every rule violation raises RuleError with a manager-friendly message, so the
web layer can just surface it. Nothing here trusts its caller — ownership,
availability, byes, and lineup legality are all re-checked against the DB.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..scoring import rules

INDIVIDUAL_POS = {"RB", "R"}
UNIT_POS = {"QB", "K", "DEF/ST", "C"}
POS_TO_NFL = {"RB": ("RB",), "R": ("WR", "TE")}   # R = WR + TE
FEE_CENTS = 200          # per transaction, once the free ones are used up
ENTRY_FEE_CENTS = 8000   # $80 season entry fee, owed by every team
FREE_TRADES = 5          # trades + opens combined that are free (in the entry fee)

# Legal weekly skill compositions -> required bye condition (or None).
#   (nRB, nR): the count of started RB and R
LEGAL_SKILL = {
    (2, 3): None,               # standard
    (3, 2): "recv_bye",         # 3rd RB allowed if >=2 receivers on bye
    (1, 4): "rb_bye",           # 4th R allowed if >=2 RBs on bye
}


class RuleError(Exception):
    """A league-rule violation, safe to show the manager."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kind_for(position: str) -> str:
    return "PLAYER" if position in INDIVIDUAL_POS else "TEAM_UNIT"


def _team_conf(conn, team_id: int) -> int:
    row = conn.execute("SELECT conference_id FROM teams WHERE id=?", (team_id,)).fetchone()
    if not row:
        raise RuleError("unknown team")
    return row["conference_id"]


# --- rosters & availability ---------------------------------------------

def current_roster(conn, team_id: int) -> list[dict]:
    # Group by canonical slot order (C, K, DEF/ST, QB, RB, R) so a newly
    # traded/opened player sits with its position instead of at the bottom.
    return [dict(r) for r in conn.execute(
        "SELECT * FROM roster_entries WHERE team_id=? AND released_ff_week IS NULL "
        "ORDER BY CASE roster_slot WHEN 'C' THEN 0 WHEN 'K' THEN 1 WHEN 'DEF/ST' THEN 2 "
        "WHEN 'QB' THEN 3 WHEN 'RB' THEN 4 WHEN 'R' THEN 5 ELSE 6 END, "
        "COALESCE(slot_order, id)",
        (team_id,))]


def _owned_refs(conn, season_id, conf_id, asset_kind, unit_type=None) -> set[str]:
    q = ("SELECT re.asset_ref FROM roster_entries re JOIN teams t ON t.id=re.team_id "
         "WHERE re.season_id=? AND t.conference_id=? AND re.released_ff_week IS NULL "
         "AND re.asset_kind=?")
    params = [season_id, conf_id, asset_kind]
    if unit_type is not None:
        q += " AND re.unit_type=?"
        params.append(unit_type)
    return {r["asset_ref"] for r in conn.execute(q, params)}


def available_players(conn, season_id, conf_id, position) -> list[dict]:
    """NFL players of a position not owned in this conference, alphabetical."""
    positions = POS_TO_NFL[position]
    owned = _owned_refs(conn, season_id, conf_id, "PLAYER")
    rows = conn.execute(
        f"SELECT gsis_id, name, position, nfl_team_abbr FROM nfl_players "
        f"WHERE season_id=? AND position IN ({','.join('?' * len(positions))}) "
        f"ORDER BY name", (season_id, *positions))
    return [dict(r) for r in rows if r["gsis_id"] not in owned]


def available_units(conn, season_id, conf_id, unit_type) -> list[dict]:
    owned = _owned_refs(conn, season_id, conf_id, "TEAM_UNIT", unit_type)
    rows = conn.execute("SELECT abbr, name FROM nfl_teams WHERE season_id=? ORDER BY name",
                        (season_id,))
    return [dict(r) for r in rows if r["abbr"] not in owned]


def _bye_week_of(conn, season_id, abbr) -> int | None:
    r = conn.execute("SELECT bye_ff_week FROM nfl_teams WHERE season_id=? AND abbr=?",
                     (season_id, abbr)).fetchone()
    return r["bye_ff_week"] if r else None


def _team_abbr_of_asset(conn, season_id, asset_kind, asset_ref) -> str | None:
    if asset_kind == "TEAM_UNIT":
        return asset_ref
    r = conn.execute("SELECT nfl_team_abbr FROM nfl_players WHERE season_id=? AND gsis_id=?",
                     (season_id, asset_ref)).fetchone()
    return r["nfl_team_abbr"] if r else None


def is_on_bye(conn, season_id, asset_kind, asset_ref, ff_week) -> bool:
    abbr = _team_abbr_of_asset(conn, season_id, asset_kind, asset_ref)
    return abbr is not None and _bye_week_of(conn, season_id, abbr) == ff_week


def _find_on_roster(conn, team_id, position, asset_ref):
    for e in current_roster(conn, team_id):
        if e["asset_ref"] == asset_ref and e["roster_slot"] == position:
            return e
    return None


def _owner_of(conn, season_id, conf_id, position, asset_ref):
    """The team in this conference holding that asset right now, or None. A team
    unit's ref is just the club (BAL), so the slot has to match too — BAL's coach
    and BAL's QB room are different assets with the same ref."""
    kind = _kind_for(position)
    return conn.execute(
        "SELECT t.id tid, t.name tname FROM roster_entries r JOIN teams t ON t.id=r.team_id "
        "WHERE r.season_id=? AND t.conference_id=? AND r.asset_ref=? AND r.asset_kind=? "
        "AND r.released_ff_week IS NULL AND (?='PLAYER' OR r.roster_slot=?) LIMIT 1",
        (season_id, conf_id, asset_ref, kind, kind, position)).fetchone()


def _took_when(conn, season_id, team_id, position, asset_ref) -> str | None:
    """When that team picked him up, Central, or None if he was drafted (a draft
    pick isn't a transaction, so there's nothing to point at)."""
    r = conn.execute(
        "SELECT created_at FROM transactions WHERE season_id=? AND team_id=? AND position=? "
        "AND in_asset_ref=? AND reversed=0 ORDER BY id DESC LIMIT 1",
        (season_id, team_id, position, asset_ref)).fetchone()
    try:
        return _ct_when(datetime.fromisoformat(r["created_at"])) if r else None
    except (TypeError, ValueError):
        return None


def _assert_available(conn, season_id, conf_id, position, asset_ref, team_id=None):
    """He's free in this conference — checked again here, against the database,
    because the list a manager is looking at is a photograph of when his screen
    loaded. Two guys can pick the same player on a Sunday morning; the second
    one to press confirm gets told who beat him to it, not "not available"
    (Scott, 2026-09-18)."""
    if position in INDIVIDUAL_POS:
        refs = {p["gsis_id"] for p in available_players(conn, season_id, conf_id, position)}
    else:
        refs = {u["abbr"] for u in available_units(conn, season_id, conf_id, position)}
    if asset_ref in refs:
        return
    kind = _kind_for(position)
    who = _asset_label(conn, season_id, kind, asset_ref, position)
    gone = " — he's no longer available" if kind == "PLAYER" else " — no longer available"
    owner = _owner_of(conn, season_id, conf_id, position, asset_ref)
    if not owner:
        # Shouldn't happen: an unowned asset is in the list. Say only what we know.
        raise RuleError("that didn't go through — this screen was out of date")
    if team_id is not None and owner["tid"] == team_id:
        # Nearly always a double-tap on a phone: the first press worked.
        raise RuleError(f"{who} is already on your roster")
    when = _took_when(conn, season_id, owner["tid"], position, asset_ref)
    raise RuleError(f"{owner['tname']} took {who} {when}{gone}" if when
                    else f"{owner['tname']} has {who}{gone}")


def _claim_lock(conn) -> bool:
    """Take SQLite's write lock before checking whether an asset is free, so two
    managers confirming at the same instant can't both pass the check and both
    end up owning him. Returns whether we opened the transaction (the caller
    rolls back only what it started)."""
    if conn.in_transaction:
        return False
    conn.execute("BEGIN IMMEDIATE")
    return True


# --- transactions --------------------------------------------------------

def _asset_label(conn, season_id, kind, ref, position) -> str:
    if kind == "PLAYER":
        p = conn.execute("SELECT name FROM nfl_players WHERE season_id=? AND gsis_id=?",
                         (season_id, ref)).fetchone()
        return p["name"] if p else ref
    return f"{ref} {position}"


def _trade_into_lineup(conn, season_id, team_id, ff_week, position, out_ref, in_ref,
                       locked_refs) -> str | None:
    """What a trade does to the week's saved lineup (commissioner, 2026-09-16).

    Only a player who was STARTING matters; a bench trade leaves the lineup be.
      * his game has started -> he stays in the lineup, locked, and keeps his
        points; the new player can't start until next week;
      * his game hasn't started and the new player's hasn't either -> a normal
        swap: the new player takes his spot;
      * his game hasn't started but the new player's has -> the new player
        can't start this week, so the spot is left open for the bench.
    locked_refs None = the commissioner, who isn't bound by kickoff: a swap.
    Returns a sentence for the manager, or None when the lineup is untouched."""
    row = conn.execute("SELECT id FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week=? "
                       "AND roster_slot=? AND asset_ref=? AND is_rental=0",
                       (season_id, team_id, ff_week, position, out_ref)).fetchone()
    if not row:
        return None
    locked = set(locked_refs or ())
    kind = _kind_for(position)
    out_name = _asset_label(conn, season_id, kind, out_ref, position)
    in_name = _asset_label(conn, season_id, kind, in_ref, position)
    if out_ref in locked:
        return (f"{out_name}'s game has already started, so he stays in your Week {ff_week} "
                f"lineup and keeps his points. {in_name} can start next week.")
    if in_ref in locked:
        conn.execute("DELETE FROM weekly_lineups WHERE id=?", (row["id"],))
        return (f"{in_name}'s game has already started, so he can't start in Week {ff_week}. "
                f"{out_name}'s spot is open — pick someone in Set Lineup.")
    conn.execute("UPDATE weekly_lineups SET asset_ref=?, asset_kind=?, unit_type=? WHERE id=?",
                 (in_ref, kind, position if position in UNIT_POS else None, row["id"]))
    return f"{in_name} takes {out_name}'s spot in your Week {ff_week} lineup."


REACQUIRE_WAIT = __import__("datetime").timedelta(hours=48)


def recently_traded_away(conn, season_id, team_id, now=None) -> dict[tuple[str, str], datetime]:
    """{(position, asset_ref): when he can come back} for everything this team
    traded away in the last 48 hours (commissioner, 2026-09-16). Without it a
    manager could trade McCaffrey for Barkley at 11:55, let Barkley's noon game
    lock him in, and trade straight back — renting Barkley for the week. A
    reversed trade doesn't count. Applies to a trade the commissioner enters too."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for r in conn.execute("SELECT position, out_asset_ref, created_at FROM transactions "
                          "WHERE season_id=? AND team_id=? AND type='TRADE' AND reversed=0",
                          (season_id, team_id)):
        try:
            back = datetime.fromisoformat(r["created_at"]) + REACQUIRE_WAIT
        except (TypeError, ValueError):
            continue                          # no usable time on the row: nothing to wait for
        if back > now:
            key = (r["position"], r["out_asset_ref"])
            out[key] = max(back, out.get(key, back))
    return out


def _ct_when(t: datetime) -> str:
    from zoneinfo import ZoneInfo
    c = t.astimezone(ZoneInfo("America/Chicago"))
    return f"{c.strftime('%a')} at {c.hour % 12 or 12}:{c.minute:02d} {'AM' if c.hour < 12 else 'PM'}"


def do_trade(conn, season_id, team_id, position, out_ref, in_ref, ff_week,
             actor=None, locked_refs=None, notes: list | None = None, now=None) -> int:
    """Permanent swap: drop out_ref, add in_ref (same position). $2 fee.

    Allowed any time. The week's saved lineup follows the trade by the kickoff
    rule (_trade_into_lineup); its sentence is appended to `notes` if given."""
    if position not in INDIVIDUAL_POS | UNIT_POS:
        raise RuleError(f"invalid position {position!r}")
    mine = _claim_lock(conn)
    try:
        out = _find_on_roster(conn, team_id, position, out_ref)
        if not out:
            raise RuleError("you don't own that player at that position")
        back = recently_traded_away(conn, season_id, team_id, now).get((position, in_ref))
        if back:
            who = _asset_label(conn, season_id, _kind_for(position), in_ref, position)
            raise RuleError(f"{who} was traded away by this team in the last 48 hours — "
                            f"he can't come back until {_ct_when(back)}")
        conf = _team_conf(conn, team_id)
        _assert_available(conn, season_id, conf, position, in_ref, team_id)

        conn.execute("UPDATE roster_entries SET released_ff_week=? WHERE id=?",
                     (ff_week, out["id"]))
        kind = _kind_for(position)
        # Take the outgoing player's exact spot within its position group.
        order = out["slot_order"] if out["slot_order"] is not None else out["id"]
        conn.execute(
            "INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,unit_type,"
            "roster_slot,acquired_ff_week,acquired_via,slot_order,created_at) "
            "VALUES (?,?,?,?,?,?,?, 'TRADE', ?, ?)",
            (season_id, team_id, kind, in_ref, position if kind == "TEAM_UNIT" else None,
             position, ff_week, order, _now()))
        note = _trade_into_lineup(conn, season_id, team_id, ff_week, position, out_ref, in_ref,
                                  locked_refs)
        if note and notes is not None:
            notes.append(note)
        return _log_tx(conn, season_id, team_id, ff_week, "TRADE", position,
                       out_ref, in_ref, kind, entered_by=actor)
    except Exception:
        if mine:                       # never leave the write lock held
            conn.rollback()
        raise


def do_open(conn, season_id, team_id, position, out_ref, in_ref, ff_week,
            actor=None, locked_refs=None) -> int:
    """One-week rental: start in_ref for out_ref THIS week only. Requires the
    outgoing player to be on his NFL bye. $2 fee. Roster is unchanged.

    locked_refs: assets whose game this week has kicked off. A rental in it is
    refused — an Open exists only to start him that week, and he can't be
    picked up once his game has started (league rule Q7). None skips the check
    (the commissioner entering a move that was phoned in before kickoff)."""
    if position not in INDIVIDUAL_POS | UNIT_POS:
        raise RuleError(f"invalid position {position!r}")
    if locked_refs and in_ref in locked_refs:
        p = conn.execute("SELECT name FROM nfl_players WHERE season_id=? AND gsis_id=?",
                         (season_id, in_ref)).fetchone()
        who = p["name"] if p else f"{in_ref} {position}"
        raise RuleError(f"{who}'s game has already started — he can't be Opened for Week {ff_week}")
    mine = _claim_lock(conn)
    try:
        out = _find_on_roster(conn, team_id, position, out_ref)
        if not out:
            raise RuleError("you don't own that player at that position")
        if not is_on_bye(conn, season_id, out["asset_kind"], out_ref, ff_week):
            raise RuleError("Open is only allowed to cover a player on his NFL bye that week")
        conf = _team_conf(conn, team_id)
        _assert_available(conn, season_id, conf, position, in_ref, team_id)
        tx = _log_tx(conn, season_id, team_id, ff_week, "OPEN", position,
                     out_ref, in_ref, _kind_for(position), entered_by=actor)
        # A lineup already saved for the week that starts the bye player now starts
        # the rental in his place (commissioner, 2026-09-15): an Open is only ever
        # bought to start it, and the manager shouldn't have to resubmit.
        conn.execute("UPDATE weekly_lineups SET asset_ref=?, asset_kind=?, unit_type=?, is_rental=1 "
                     "WHERE season_id=? AND team_id=? AND ff_week=? AND roster_slot=? "
                     "AND asset_ref=? AND is_rental=0",
                     (in_ref, _kind_for(position), position if position in UNIT_POS else None,
                      season_id, team_id, ff_week, position, out_ref))
        conn.commit()
        return tx
    except Exception:
        if mine:
            conn.rollback()
        raise


DRAFT_SLOT_MAX = {"C": 1, "K": 1, "DEF/ST": 1, "QB": 1, "RB": 3, "R": 4}


def draft_player(conn, season_id, team_id, kind, ref, slot) -> None:
    """Assign a drafted asset to a team's roster, fee-free (acquired_via=DRAFT).
    Enforces the roster template (max per slot) and the division's separate pool
    (can't draft an asset already taken in that conference)."""
    if slot not in DRAFT_SLOT_MAX:
        raise RuleError(f"invalid slot {slot!r}")
    n = conn.execute("SELECT COUNT(*) c FROM roster_entries WHERE season_id=? AND team_id=? "
                     "AND roster_slot=? AND released_ff_week IS NULL",
                     (season_id, team_id, slot)).fetchone()["c"]
    if n >= DRAFT_SLOT_MAX[slot]:
        raise RuleError(f"that team's {slot} slot is already full")
    conf = _team_conf(conn, team_id)
    unit = slot if kind == "TEAM_UNIT" else None
    taken = (_owned_refs(conn, season_id, conf, "TEAM_UNIT", unit) if kind == "TEAM_UNIT"
             else _owned_refs(conn, season_id, conf, "PLAYER"))
    if ref in taken:
        raise RuleError("already drafted in this division")
    # The draft board is built from nflverse's roster file; the league's own
    # player list is refreshed daily from the same place but can be a day
    # behind it, and a rookie signed since is on the board and not in the
    # league. Drafting him would store an id nothing can turn back into a name,
    # so say so instead (Scott, 2026-09-19).
    if kind == "PLAYER":
        known = conn.execute("SELECT 1 FROM nfl_players WHERE season_id=? AND gsis_id=?",
                             (season_id, ref)).fetchone()
        if not known:
            raise RuleError(f"{ref} isn't in the league's player list yet — run "
                            f"`manage.py refresh-players` and try again")
    else:
        known = conn.execute("SELECT 1 FROM nfl_teams WHERE season_id=? AND abbr=?",
                             (season_id, ref)).fetchone()
        if not known:
            raise RuleError(f"{ref} isn't an NFL team in this season")
    conn.execute("INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,unit_type,"
                 "roster_slot,acquired_ff_week,acquired_via,created_at) "
                 "VALUES (?,?,?,?,?,?,0,'DRAFT',?)",
                 (season_id, team_id, kind, ref, unit, slot, _now()))
    conn.commit()


# ---- season setup lock ---------------------------------------------------
# Team name + team_number fix a team's identity and its 15-week schedule, so
# once the commissioner has entered them on draft day they get locked. Unlocking
# is a deliberate, confirmed step (the UI warns that changing a team_number
# regenerates the schedule).
def _lock_key(season_id) -> str:
    return f"setup_locked:{season_id}"


def is_setup_locked(conn, season_id) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (_lock_key(season_id),)).fetchone()
    return bool(row) and row["value"] == "1"


def set_setup_locked(conn, season_id, locked: bool) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                 (_lock_key(season_id), "1" if locked else "0"))
    conn.commit()


# ---- draft order ---------------------------------------------------------

def set_draft_slots(conn, season_id, conf_id, mapping) -> int:
    """Assign draft slots to a conference's teams. mapping = {team_id: slot}.
    Slots are the card draw (1..N, unique within the conference). Applied in two
    phases because of UNIQUE(season_id, conference_id, draft_slot) — clearing
    first lets teams swap slots without a transient collision."""
    rows = conn.execute("SELECT id FROM teams WHERE season_id=? AND conference_id=?",
                        (season_id, conf_id)).fetchall()
    valid = {r["id"] for r in rows}
    n = len(valid)
    clean = {}
    for tid, slot in mapping.items():
        tid = int(tid)
        if tid not in valid:
            raise RuleError("that team isn't in this conference")
        if slot in (None, ""):
            continue
        slot = int(slot)
        if not 1 <= slot <= n:
            raise RuleError(f"slot {slot} is out of range (1-{n})")
        clean[tid] = slot
    if len(set(clean.values())) != len(clean):
        raise RuleError("two teams can't share the same draft slot")
    conn.execute("UPDATE teams SET draft_slot=NULL WHERE season_id=? AND conference_id=?",
                 (season_id, conf_id))
    for tid, slot in clean.items():
        conn.execute("UPDATE teams SET draft_slot=? WHERE id=?", (slot, tid))
    conn.commit()
    return len(clean)


# ---- draft clock ---------------------------------------------------------
# "Who is on the clock" is a cursor into the draft sequence, persisted per
# season+conference in `settings`. It is deliberately DECOUPLED from roster
# contents: editing a roster or assigning an out-of-turn pick to a specific
# team never moves the clock; only an on-the-clock pick (or the manual clock
# controls) advances it.
def _cursor_key(season_id, conf_id) -> str:
    return f"draft_cursor:{season_id}:{conf_id}"


def get_draft_cursor(conn, season_id, conf_id):
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (_cursor_key(season_id, conf_id),)).fetchone()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def set_draft_cursor(conn, season_id, conf_id, pick: int) -> int:
    pick = max(1, int(pick))
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                 (_cursor_key(season_id, conf_id), str(pick)))
    conn.commit()
    return pick


def draft_pick_count(conn, season_id, conf_id) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM roster_entries re JOIN teams t ON t.id=re.team_id "
        "WHERE re.season_id=? AND t.conference_id=? AND re.acquired_via='DRAFT'",
        (season_id, conf_id)).fetchone()["c"]


def undo_last_draft(conn, season_id, conf_id) -> str | None:
    """Take back the most recent on-the-clock pick: remove the newest DRAFT
    entry AND step the clock back one. (For surgical roster fixes that must not
    move the clock, use remove_draft_entry instead.) Returns the ref removed."""
    row = conn.execute(
        "SELECT re.id, re.asset_ref FROM roster_entries re JOIN teams t ON t.id=re.team_id "
        "WHERE re.season_id=? AND t.conference_id=? AND re.acquired_via='DRAFT' "
        "ORDER BY re.id DESC LIMIT 1", (season_id, conf_id)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM roster_entries WHERE id=?", (row["id"],))
    cur = get_draft_cursor(conn, season_id, conf_id)
    if cur is not None and cur > 1:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                     (_cursor_key(season_id, conf_id), str(cur - 1)))
    conn.commit()
    return row["asset_ref"]


def remove_draft_entry(conn, season_id, conf_id, entry_id) -> str | None:
    """Remove one DRAFT pick by id (a roster edit). Does NOT touch the clock."""
    row = conn.execute(
        "SELECT re.id, re.asset_ref FROM roster_entries re JOIN teams t ON t.id=re.team_id "
        "WHERE re.id=? AND re.season_id=? AND t.conference_id=? AND re.acquired_via='DRAFT'",
        (entry_id, season_id, conf_id)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM roster_entries WHERE id=?", (row["id"],))
    conn.commit()
    return row["asset_ref"]


def reset_draft(conn, season_id, conf_id) -> int:
    """Clear every DRAFT pick in a conference and reset the clock. Returns count."""
    cur = conn.execute(
        "DELETE FROM roster_entries WHERE id IN (SELECT re.id FROM roster_entries re "
        "JOIN teams t ON t.id=re.team_id WHERE re.season_id=? AND t.conference_id=? "
        "AND re.acquired_via='DRAFT')", (season_id, conf_id))
    conn.execute("DELETE FROM settings WHERE key=?", (_cursor_key(season_id, conf_id),))
    conn.commit()
    return cur.rowcount


def reverse_transaction(conn, tx_id) -> None:
    """Undo a transaction (commissioner). A TRADE restores the roster (bring the
    dropped player back, remove the added one); an OPEN drops its rental from
    that week's lineup. The row is marked reversed either way. Idempotent."""
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx or tx["reversed"]:
        return
    if tx["type"] == "TRADE":
        conn.execute("DELETE FROM roster_entries WHERE team_id=? AND asset_ref=? AND "
                     "acquired_via='TRADE' AND acquired_ff_week=? AND released_ff_week IS NULL",
                     (tx["team_id"], tx["in_asset_ref"], tx["ff_week"]))
        conn.execute("UPDATE roster_entries SET released_ff_week=NULL WHERE team_id=? AND "
                     "asset_ref=? AND released_ff_week=?",
                     (tx["team_id"], tx["out_asset_ref"], tx["ff_week"]))
    else:  # OPEN — the rental's start goes back to the player it covered
        key = (tx["season_id"], tx["team_id"], tx["ff_week"])
        back = conn.execute("SELECT 1 FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week=? "
                            "AND roster_slot=? AND asset_ref=?",
                            (*key, tx["position"], tx["out_asset_ref"])).fetchone()
        if back:   # he's already starting there, so the rental's row just goes
            conn.execute("DELETE FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week=? "
                         "AND asset_ref=? AND is_rental=1", (*key, tx["in_asset_ref"]))
        else:
            conn.execute("UPDATE weekly_lineups SET asset_ref=?, asset_kind=?, unit_type=?, is_rental=0 "
                         "WHERE season_id=? AND team_id=? AND ff_week=? AND roster_slot=? "
                         "AND asset_ref=? AND is_rental=1",
                         (tx["out_asset_ref"], tx["out_asset_kind"],
                          tx["position"] if tx["position"] in UNIT_POS else None,
                          *key, tx["position"], tx["in_asset_ref"]))
    conn.execute("UPDATE transactions SET reversed=1 WHERE id=?", (tx_id,))
    conn.commit()


def convert_transaction(conn, season_id, tx_id) -> int:
    """Flip a transaction between TRADE and OPEN (commissioner fix for the
    'forgot to toggle Open' mistake): reverse the original, then redo it as the
    other type with the same players. Returns the new transaction id."""
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        raise RuleError("no such transaction")
    if tx["reversed"]:
        raise RuleError("that transaction was already reversed")
    reverse_transaction(conn, tx_id)
    args = (conn, season_id, tx["team_id"], tx["position"],
            tx["out_asset_ref"], tx["in_asset_ref"], tx["ff_week"])
    return do_open(*args) if tx["type"] == "TRADE" else do_trade(*args)


def _tx_count(conn, team_id) -> int:
    """How many trades+opens this team has made (non-reversed)."""
    return conn.execute("SELECT COUNT(*) c FROM transactions WHERE team_id=? AND reversed=0",
                        (team_id,)).fetchone()["c"]


def _log_tx(conn, season_id, team_id, ff_week, typ, position, out_ref, in_ref, in_kind,
            entered_by=None) -> int:
    kind_out = _kind_for(position)
    fee = 0 if _tx_count(conn, team_id) < FREE_TRADES else FEE_CENTS   # first 5 are free
    cur = conn.execute(
        "INSERT INTO transactions(season_id,team_id,ff_week,type,position,"
        "out_asset_kind,out_asset_ref,in_asset_kind,in_asset_ref,fee_cents,"
        "entered_by,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (season_id, team_id, ff_week, typ, position, kind_out, out_ref, in_kind,
         in_ref, fee, entered_by, _now()))
    conn.commit()
    return int(cur.lastrowid)


def _open_rentals(conn, season_id, team_id, ff_week) -> dict[str, str]:
    """{in_asset_ref: position} the team may start this week via an OPEN."""
    return {r["in_asset_ref"]: r["position"] for r in conn.execute(
        "SELECT in_asset_ref, position FROM transactions WHERE season_id=? AND team_id=? "
        "AND ff_week=? AND type='OPEN' AND reversed=0", (season_id, team_id, ff_week))}


# --- lineups (with bye-flex) --------------------------------------------

def _bye_count(conn, season_id, team_id, slot, ff_week) -> int:
    """How many of this team's rostered players at `slot` are on bye this week
    and NOT covered by an Open. A covered player doesn't count toward the
    bye-week flex: the Open already filled the hole the flex exists to fill."""
    covered = {r["out_asset_ref"] for r in conn.execute(
        "SELECT out_asset_ref FROM transactions WHERE season_id=? AND team_id=? "
        "AND ff_week=? AND type='OPEN' AND reversed=0", (season_id, team_id, ff_week))}
    n = 0
    for e in current_roster(conn, team_id):
        if (e["roster_slot"] == slot and e["asset_ref"] not in covered
                and is_on_bye(conn, season_id, e["asset_kind"], e["asset_ref"], ff_week)):
            n += 1
    return n


UNIT_FLEX = ("QB", "K")    # team slots an extra RB/receiver may fill while on bye


def unit_on_bye_uncovered(conn, season_id, team_id, slot, ff_week) -> bool:
    """This team's QB (or K) is on bye this week and no Open covers it — the
    slot may then be filled by a 3rd RB or 4th receiver instead (commissioner,
    2026-09-16). A covered unit doesn't qualify: the Open already fills it."""
    covered = {r["out_asset_ref"] for r in conn.execute(
        "SELECT out_asset_ref FROM transactions WHERE season_id=? AND team_id=? AND ff_week=? "
        "AND type='OPEN' AND reversed=0 AND position=?", (season_id, team_id, ff_week, slot))}
    return any(e["roster_slot"] == slot and e["asset_ref"] not in covered
               and is_on_bye(conn, season_id, e["asset_kind"], e["asset_ref"], ff_week)
               for e in current_roster(conn, team_id))


def bye_flex(conn, season_id, team_id, ff_week) -> dict:
    """Which bye-week flex this team may use this week — the same test set_lineup
    applies, so Set Lineup's count agrees with what submitting will accept.
    {"RB": a 3rd RB is allowed, "R": a 4th receiver is allowed,
     "QB"/"K": that slot may be left to an extra RB or receiver}"""
    least = rules.BYE_FLEX_MIN_ON_BYE
    out = {"RB": _bye_count(conn, season_id, team_id, "R", ff_week) >= least,
           "R": _bye_count(conn, season_id, team_id, "RB", ff_week) >= least}
    for slot in UNIT_FLEX:
        out[slot] = unit_on_bye_uncovered(conn, season_id, team_id, slot, ff_week)
    return out


def lineup_problem(conn, season_id, team_id, ff_week, slots: list[str]) -> str | None:
    """Why a lineup with these starting slots isn't legal this week, or None.

    Nine starters: a C and a DEF/ST always; a QB and a K unless that unit is on
    bye and uncovered, when its slot goes to an extra RB or receiver; and the RBs
    and receivers make up the rest — 2 RB + 3 R, or the bye-week flex (3 + 2
    with 2+ receivers on bye, 1 + 4 with 2+ RBs on bye), plus one RB or receiver
    of the manager's choosing for each QB/K slot given up."""
    for unit in ("C", "DEF/ST"):
        if slots.count(unit) != 1:
            return f"you must start exactly one {unit}"
    flex = bye_flex(conn, season_id, team_id, ff_week)
    extras = 0
    for unit in UNIT_FLEX:
        n = slots.count(unit)
        if n > 1 or (n == 0 and not flex[unit]):
            return (f"you must start exactly one {unit}" + (
                "" if n else f" — you can start an extra RB or receiver instead only when "
                             f"your {unit} is on bye and not covered by an Open"))
        extras += n == 0
    n_rb, n_r = slots.count("RB"), slots.count("R")
    if len(slots) != 9 or n_rb + n_r != 5 + extras:
        return "a lineup is 9 starters: C, K, DEF/ST, QB, 2 RB and 3 receivers"
    needs = set()
    for x in range(extras + 1):                       # x of the extras are RBs
        base = (n_rb - x, n_r - (extras - x))
        if base not in LEGAL_SKILL:
            continue
        need = LEGAL_SKILL[base]
        if need is None or (need == "recv_bye" and flex["RB"]) or (need == "rb_bye" and flex["R"]):
            return None
        needs.add(need)
    if "recv_bye" in needs:
        return ("you can only start a 3rd RB when 2 or more of your receivers are on bye, or "
                "in place of a QB or K on bye (a player covered by an Open doesn't count)")
    if "rb_bye" in needs:
        return ("you can only start a 4th receiver when 2 or more of your RBs are on bye, or "
                "in place of a QB or K on bye (a player covered by an Open doesn't count)")
    return f"{n_rb} RB + {n_r} R isn't a legal lineup"


def set_lineup(conn, season_id, team_id, ff_week, starters: list[dict],
               locked_refs=None, submitted_by=None) -> None:
    """starters: list of {roster_slot, asset_ref}. Validates the 9-man lineup,
    the bye-flex composition, that every starter is owned or a valid rental, and
    (if locked_refs given) that no player whose game has kicked off is being
    started or benched.
    """
    problem = lineup_problem(conn, season_id, team_id, ff_week,
                             [s["roster_slot"] for s in starters])
    if problem:
        raise RuleError(problem)

    # A player traded IN this week can only start if the player he replaced
    # hadn't played yet (commissioner, 2026-09-16). Otherwise the team would
    # have both: the man it traded away, locked into this week's lineup with his
    # points, and his replacement starting somewhere else — an extra player for
    # the week (Scott, 2026-09-18).
    if locked_refs:
        locked = set(locked_refs)
        started = {(s["roster_slot"], s["asset_ref"]) for s in starters}
        for t in conn.execute(
                "SELECT position, out_asset_ref, in_asset_ref FROM transactions WHERE season_id=? "
                "AND team_id=? AND ff_week=? AND type='TRADE' AND reversed=0",
                (season_id, team_id, ff_week)):
            if t["out_asset_ref"] in locked and (t["position"], t["in_asset_ref"]) in started:
                came_in = _asset_label(conn, season_id, _kind_for(t["position"]),
                                       t["in_asset_ref"], t["position"])
                went_out = _asset_label(conn, season_id, _kind_for(t["position"]),
                                        t["out_asset_ref"], t["position"])
                raise RuleError(
                    f"{came_in} can't start this week — {went_out}'s game had already started when "
                    f"you traded him, so he keeps week {ff_week} and {came_in} starts next week")

    rentals = _open_rentals(conn, season_id, team_id, ff_week)
    # A starter traded away after his game kicked off stays in this week's
    # lineup (he keeps his points), so a resubmitted lineup may still name him.
    kept = {(r["roster_slot"], r["asset_ref"]): r for r in conn.execute(
        "SELECT roster_slot, asset_ref, asset_kind, unit_type FROM weekly_lineups "
        "WHERE season_id=? AND team_id=? AND ff_week=? AND is_rental=0",
        (season_id, team_id, ff_week))}
    resolved = []
    for s in starters:
        slot, ref = s["roster_slot"], s["asset_ref"]
        on = _find_on_roster(conn, team_id, slot, ref)
        if on:
            resolved.append((slot, on["asset_kind"], ref, on["unit_type"], 0))
        elif rentals.get(ref) == slot:
            resolved.append((slot, _kind_for(slot), ref,
                             slot if slot in UNIT_POS else None, 1))
        elif (slot, ref) in kept and (locked_refs is None or ref in set(locked_refs)):
            k = kept[(slot, ref)]
            resolved.append((slot, k["asset_kind"], ref, k["unit_type"], 0))
        else:
            raise RuleError(f"{ref} isn't on your roster or an active Open for week {ff_week}")

    if locked_refs:
        current = {r["asset_ref"] for r in conn.execute(
            "SELECT asset_ref FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week=?",
            (season_id, team_id, ff_week))}
        new = {s["asset_ref"] for s in starters}
        if (current ^ new) & set(locked_refs):     # a locked asset changed status
            raise RuleError("those players' games have already started — you can't change them now")

    conn.execute("DELETE FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week=?",
                 (season_id, team_id, ff_week))
    for slot, kind, ref, unit, rental in resolved:
        conn.execute(
            "INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,"
            "asset_ref,unit_type,is_rental,submitted_at,submitted_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (season_id, team_id, ff_week, slot, kind, ref, unit, rental, _now(),
             submitted_by))
    conn.commit()


# --- fees ----------------------------------------------------------------

def fee_balance_cents(conn, team_id) -> dict:
    tx_fees = conn.execute("SELECT COALESCE(SUM(fee_cents),0) s FROM transactions "
                           "WHERE team_id=? AND reversed=0", (team_id,)).fetchone()["s"]
    paid = conn.execute("SELECT COALESCE(SUM(amount_cents),0) s FROM payments "
                        "WHERE team_id=?", (team_id,)).fetchone()["s"]
    owed = ENTRY_FEE_CENTS + tx_fees                 # $80 entry + any paid transactions
    used = _tx_count(conn, team_id)
    return {"entry_fee_cents": ENTRY_FEE_CENTS, "tx_fee_cents": tx_fees,
            "owed_cents": owed, "paid_cents": paid, "balance_cents": owed - paid,
            "free_used": min(used, FREE_TRADES), "free_left": max(0, FREE_TRADES - used)}


def record_payment(conn, season_id, team_id, amount_cents, note=None, actor=None) -> None:
    conn.execute(
        "INSERT INTO payments(season_id,team_id,amount_cents,note,applied_by,applied_at) "
        "VALUES (?,?,?,?,?,?)", (season_id, team_id, amount_cents, note, actor, _now()))
    conn.commit()


def transaction_history(conn, team_id) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE team_id=? ORDER BY ff_week DESC, id DESC",
        (team_id,))]
