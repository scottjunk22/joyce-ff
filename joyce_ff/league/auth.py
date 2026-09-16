"""
Passcode auth for the league site.

Low-stakes but done right: passcodes are stored only as salted PBKDF2 hashes
(stdlib — no dependency), verified in constant time. A team's passcode gates
edits to that team; a commissioner passcode gates admin actions. Co-managers
(e.g. Scott & Drew on OT Blitz) simply share the team passcode.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGO = "pbkdf2_sha256"
_ITERS = 200_000


def hash_passcode(passcode: str) -> str:
    if not passcode:
        raise ValueError("passcode must be non-empty")
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", passcode.encode(), bytes.fromhex(salt), _ITERS)
    return f"{_ALGO}${_ITERS}${salt}${dk.hex()}"


def verify_passcode(passcode: str, stored: str | None) -> bool:
    if not stored or not passcode:
        return False
    try:
        algo, iters, salt, expected = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", passcode.encode(),
                                 bytes.fromhex(salt), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), expected)


# --- DB-backed helpers ---------------------------------------------------

# --- manager PINs -------------------------------------------------------
# A team's credential is a 4-6 digit PIN the manager sets themselves. It is
# hashed like any other secret: the commissioner can RESET a PIN but can never
# read one back, so there is no master list to leak.
PIN_MIN, PIN_MAX = 4, 6


class PinError(ValueError):
    """A PIN that doesn't meet the rules, safe to show the manager."""


def validate_pin(pin: str) -> str:
    pin = (pin or "").strip()
    if not pin.isdigit():
        raise PinError("PIN must be numbers only")
    if not PIN_MIN <= len(pin) <= PIN_MAX:
        raise PinError(f"PIN must be {PIN_MIN}-{PIN_MAX} digits")
    return pin


def team_has_pin(conn, team_id: int) -> bool:
    row = conn.execute("SELECT passcode_hash FROM teams WHERE id=?", (team_id,)).fetchone()
    return bool(row and row["passcode_hash"])


# A team is OPEN for PIN setup only when the commissioner opens it — one team,
# or every PIN-less team in a conference at its draft — and it stays open until
# its manager sets a PIN, then closes on its own (commissioner, 2026-09-16:
# many managers set theirs at home after the draft). There is no league-wide
# switch: at the Blue draft, Red teams must not be claimable. A reset is the
# same thing for a team that had a PIN. The commissioner never picks or learns
# a manager's PIN.

def _pin_open_key(season_id: int, team_id: int) -> str:
    return f"pin_reset:{season_id}:{team_id}"          # key name predates opening by conference


def open_team_pin(conn, season_id: int, team_id: int, kind: str = "open") -> None:
    """Open one team; kind is 'open' (never had a PIN) or 'reset'."""
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                 (_pin_open_key(season_id, team_id), kind))
    conn.commit()


def reset_team_pin(conn, season_id: int, team_id: int) -> None:
    conn.execute("UPDATE teams SET passcode_hash=NULL WHERE id=?", (team_id,))
    open_team_pin(conn, season_id, team_id, "reset")


def open_conference_pins(conn, season_id: int, conference_code: str) -> int:
    """Open every team in the conference that has no PIN. Returns how many."""
    ids = [r["id"] for r in conn.execute(
        "SELECT t.id FROM teams t JOIN conferences c ON c.id=t.conference_id "
        "WHERE t.season_id=? AND c.code=? AND t.passcode_hash IS NULL", (season_id, conference_code))]
    for tid in ids:
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,'open')",
                     (_pin_open_key(season_id, tid),))
    conn.commit()
    return len(ids)


def close_team_pin(conn, season_id: int, team_id: int) -> None:
    conn.execute("DELETE FROM settings WHERE key=?", (_pin_open_key(season_id, team_id),))
    conn.commit()


def close_all_pins(conn, season_id: int) -> None:
    conn.execute("DELETE FROM settings WHERE key LIKE ?", (f"pin_reset:{season_id}:%",))
    conn.commit()


def pin_opens(conn, season_id: int) -> dict[int, str]:
    """{team_id: 'open' | 'reset'} for teams waiting on their manager to set a PIN."""
    prefix = f"pin_reset:{season_id}:"
    return {int(r["key"][len(prefix):]): ("reset" if r["value"] == "reset" else "open")
            for r in conn.execute("SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",))}


def claim_team_pin(conn, season_id: int, team_id: int, pin: str) -> None:
    """A manager sets their own PIN. Only possible while the commissioner has
    this team open and it has no PIN — so an unclaimed team isn't left open to
    whoever wanders by."""
    if team_id not in pin_opens(conn, season_id):
        raise PinError("PIN setup isn't open for this team — ask the commissioner to open it")
    if team_has_pin(conn, team_id):
        raise PinError("this team already has a PIN — use Change PIN, or ask the "
                       "commissioner to reset it")
    pin = validate_pin(pin)
    conn.execute("UPDATE teams SET passcode_hash=? WHERE id=?", (hash_passcode(pin), team_id))
    conn.execute("DELETE FROM settings WHERE key=?", (_pin_open_key(season_id, team_id),))
    conn.commit()


def change_team_pin(conn, team_id: int, current_pin: str, new_pin: str) -> None:
    """Self-service change. Requires the current PIN; forgotten PINs go through
    the commissioner's reset instead."""
    if not check_team_passcode(conn, team_id, current_pin):
        raise PinError("that's not your current PIN")
    new_pin = validate_pin(new_pin)
    conn.execute("UPDATE teams SET passcode_hash=? WHERE id=?",
                 (hash_passcode(new_pin), team_id))
    conn.commit()


def set_team_passcode(conn, team_id: int, passcode: str) -> None:
    conn.execute("UPDATE teams SET passcode_hash=? WHERE id=?",
                 (hash_passcode(passcode), team_id))
    conn.commit()


def check_team_passcode(conn, team_id: int, passcode: str) -> bool:
    row = conn.execute("SELECT passcode_hash FROM teams WHERE id=?", (team_id,)).fetchone()
    return bool(row) and verify_passcode(passcode, row["passcode_hash"])


def set_admin_passcode(conn, name: str, passcode: str) -> None:
    conn.execute("UPDATE admins SET passcode_hash=? WHERE name=?",
                 (hash_passcode(passcode), name))
    conn.commit()


def commissioner_name(conn, passcode: str) -> str | None:
    """Which commissioner this passcode belongs to, or None. Used to record who
    entered a move made on a manager's behalf."""
    for row in conn.execute("SELECT name, passcode_hash FROM admins"):
        if verify_passcode(passcode, row["passcode_hash"]):
            return row["name"]
    return None


def is_commissioner(conn, passcode: str) -> bool:
    """True if the passcode matches ANY commissioner (Steve or Scott)."""
    return commissioner_name(conn, passcode) is not None


# --- private OT-Blitz platform (draft board etc.) — Scott's eyes only -----

def set_platform_passcode(conn, passcode: str) -> None:
    """Gate for the private OT-Blitz platform. Stored hashed in settings; kept
    separate from team/commissioner passcodes so valuations never leak."""
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('otblitz_pc',?)",
                 (hash_passcode(passcode),))
    conn.commit()


def check_platform_passcode(conn, passcode: str) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key='otblitz_pc'").fetchone()
    return bool(row) and verify_passcode(passcode, row["value"])
