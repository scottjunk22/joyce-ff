"""
Repository layer: the operations on the league DB that ENFORCE the rules.

Every rule violation raises RuleError with a manager-friendly message, so the
web layer can just surface it. Nothing here trusts its caller — ownership,
availability, byes, and lineup legality are all re-checked against the DB.
"""

from __future__ import annotations

from datetime import datetime, timezone

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


def _assert_available(conn, season_id, conf_id, position, asset_ref):
    if position in INDIVIDUAL_POS:
        refs = {p["gsis_id"] for p in available_players(conn, season_id, conf_id, position)}
    else:
        refs = {u["abbr"] for u in available_units(conn, season_id, conf_id, position)}
    if asset_ref not in refs:
        raise RuleError("that player isn't available in your conference")


# --- transactions --------------------------------------------------------

def do_trade(conn, season_id, team_id, position, out_ref, in_ref, ff_week,
             actor="manager") -> int:
    """Permanent swap: drop out_ref, add in_ref (same position). $2 fee."""
    if position not in INDIVIDUAL_POS | UNIT_POS:
        raise RuleError(f"invalid position {position!r}")
    out = _find_on_roster(conn, team_id, position, out_ref)
    if not out:
        raise RuleError("you don't own that player at that position")
    conf = _team_conf(conn, team_id)
    _assert_available(conn, season_id, conf, position, in_ref)

    conn.execute("UPDATE roster_entries SET released_ff_week=? WHERE id=?", (ff_week, out["id"]))
    kind = _kind_for(position)
    # Take the outgoing player's exact spot within its position group.
    order = out["slot_order"] if out["slot_order"] is not None else out["id"]
    conn.execute(
        "INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,unit_type,"
        "roster_slot,acquired_ff_week,acquired_via,slot_order,created_at) "
        "VALUES (?,?,?,?,?,?,?, 'TRADE', ?, ?)",
        (season_id, team_id, kind, in_ref, position if kind == "TEAM_UNIT" else None,
         position, ff_week, order, _now()))
    return _log_tx(conn, season_id, team_id, ff_week, "TRADE", position,
                   out_ref, in_ref, kind)


def do_open(conn, season_id, team_id, position, out_ref, in_ref, ff_week,
            actor="manager") -> int:
    """One-week rental: start in_ref for out_ref THIS week only. Requires the
    outgoing player to be on his NFL bye. $2 fee. Roster is unchanged."""
    if position not in INDIVIDUAL_POS | UNIT_POS:
        raise RuleError(f"invalid position {position!r}")
    out = _find_on_roster(conn, team_id, position, out_ref)
    if not out:
        raise RuleError("you don't own that player at that position")
    if not is_on_bye(conn, season_id, out["asset_kind"], out_ref, ff_week):
        raise RuleError("Open is only allowed to cover a player on his NFL bye that week")
    conf = _team_conf(conn, team_id)
    _assert_available(conn, season_id, conf, position, in_ref)
    return _log_tx(conn, season_id, team_id, ff_week, "OPEN", position,
                   out_ref, in_ref, _kind_for(position))


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
    conn.execute("INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,unit_type,"
                 "roster_slot,acquired_ff_week,acquired_via,created_at) "
                 "VALUES (?,?,?,?,?,?,0,'DRAFT',?)",
                 (season_id, team_id, kind, ref, unit, slot, _now()))
    conn.commit()


def undo_last_draft(conn, season_id, conf_id) -> str | None:
    """Remove the most recent DRAFT pick in a conference. Returns the ref removed."""
    row = conn.execute(
        "SELECT re.id, re.asset_ref FROM roster_entries re JOIN teams t ON t.id=re.team_id "
        "WHERE re.season_id=? AND t.conference_id=? AND re.acquired_via='DRAFT' "
        "ORDER BY re.id DESC LIMIT 1", (season_id, conf_id)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM roster_entries WHERE id=?", (row["id"],))
    conn.commit()
    return row["asset_ref"]


def reset_draft(conn, season_id, conf_id) -> int:
    """Clear every DRAFT pick in a conference (start a draft fresh). Returns count."""
    cur = conn.execute(
        "DELETE FROM roster_entries WHERE id IN (SELECT re.id FROM roster_entries re "
        "JOIN teams t ON t.id=re.team_id WHERE re.season_id=? AND t.conference_id=? "
        "AND re.acquired_via='DRAFT')", (season_id, conf_id))
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
    else:  # OPEN — pull the rental out of that week's lineup if it was started
        conn.execute("DELETE FROM weekly_lineups WHERE season_id=? AND team_id=? AND ff_week=? "
                     "AND asset_ref=? AND is_rental=1",
                     (tx["season_id"], tx["team_id"], tx["ff_week"], tx["in_asset_ref"]))
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


def _log_tx(conn, season_id, team_id, ff_week, typ, position, out_ref, in_ref, in_kind) -> int:
    kind_out = _kind_for(position)
    fee = 0 if _tx_count(conn, team_id) < FREE_TRADES else FEE_CENTS   # first 5 are free
    cur = conn.execute(
        "INSERT INTO transactions(season_id,team_id,ff_week,type,position,"
        "out_asset_kind,out_asset_ref,in_asset_kind,in_asset_ref,fee_cents,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (season_id, team_id, ff_week, typ, position, kind_out, out_ref, in_kind,
         in_ref, fee, _now()))
    conn.commit()
    return int(cur.lastrowid)


def _open_rentals(conn, season_id, team_id, ff_week) -> dict[str, str]:
    """{in_asset_ref: position} the team may start this week via an OPEN."""
    return {r["in_asset_ref"]: r["position"] for r in conn.execute(
        "SELECT in_asset_ref, position FROM transactions WHERE season_id=? AND team_id=? "
        "AND ff_week=? AND type='OPEN' AND reversed=0", (season_id, team_id, ff_week))}


# --- lineups (with bye-flex) --------------------------------------------

def _bye_count(conn, season_id, team_id, slot, ff_week) -> int:
    n = 0
    for e in current_roster(conn, team_id):
        if e["roster_slot"] == slot and is_on_bye(conn, season_id, e["asset_kind"],
                                                   e["asset_ref"], ff_week):
            n += 1
    return n


def set_lineup(conn, season_id, team_id, ff_week, starters: list[dict],
               locked_refs=None) -> None:
    """starters: list of {roster_slot, asset_ref}. Validates the 9-man lineup,
    the bye-flex composition, that every starter is owned or a valid rental, and
    (if locked_refs given) that no player whose game has kicked off is being
    started or benched.
    """
    slots = [s["roster_slot"] for s in starters]
    for unit in ("C", "K", "DEF/ST", "QB"):
        if slots.count(unit) != 1:
            raise RuleError(f"you must start exactly one {unit}")
    n_rb, n_r = slots.count("RB"), slots.count("R")
    if n_rb + n_r != 5 or len(starters) != 9:
        raise RuleError("a lineup is 9 starters: C, K, DEF/ST, QB and 5 of RB/R")

    need = LEGAL_SKILL.get((n_rb, n_r), "ILLEGAL")
    if need == "ILLEGAL":
        raise RuleError(f"{n_rb} RB + {n_r} R isn't a legal lineup")
    if need == "recv_bye" and _bye_count(conn, season_id, team_id, "R", ff_week) < 2:
        raise RuleError("you can only start a 3rd RB when 2 of your receivers are on bye")
    if need == "rb_bye" and _bye_count(conn, season_id, team_id, "RB", ff_week) < 2:
        raise RuleError("you can only start a 4th receiver when 2 of your RBs are on bye")

    rentals = _open_rentals(conn, season_id, team_id, ff_week)
    resolved = []
    for s in starters:
        slot, ref = s["roster_slot"], s["asset_ref"]
        on = _find_on_roster(conn, team_id, slot, ref)
        if on:
            resolved.append((slot, on["asset_kind"], ref, on["unit_type"], 0))
        elif rentals.get(ref) == slot:
            resolved.append((slot, _kind_for(slot), ref,
                             slot if slot in UNIT_POS else None, 1))
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
            "asset_ref,unit_type,is_rental,submitted_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (season_id, team_id, ff_week, slot, kind, ref, unit, rental, _now()))
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
