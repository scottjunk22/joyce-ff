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


def _pin_window_key(season_id: int) -> str:
    return f"pin_setup_open:{season_id}"


def pin_setup_open(conn, season_id: int) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (_pin_window_key(season_id),)).fetchone()
    return bool(row) and row["value"] == "1"


def set_pin_setup_open(conn, season_id: int, is_open: bool) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                 (_pin_window_key(season_id), "1" if is_open else "0"))
    conn.commit()


def claim_team_pin(conn, season_id: int, team_id: int, pin: str,
                   manager_names: str | None = None) -> None:
    """First-time claim: a manager sets their own PIN. Only possible while the
    commissioner has the setup window open AND the team has no PIN yet — so an
    unclaimed team is never left open to whoever wanders by."""
    if not pin_setup_open(conn, season_id):
        raise PinError("PIN setup isn't open right now — ask the commissioner to open it")
    if team_has_pin(conn, team_id):
        raise PinError("this team already has a PIN — use Change PIN, or ask the "
                       "commissioner to reset it")
    pin = validate_pin(pin)
    conn.execute("UPDATE teams SET passcode_hash=? WHERE id=?", (hash_passcode(pin), team_id))
    if manager_names:
        conn.execute("UPDATE teams SET manager_names=? WHERE id=?",
                     (manager_names.strip()[:80], team_id))
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


def is_commissioner(conn, passcode: str) -> bool:
    """True if the passcode matches ANY commissioner (Steve or Scott)."""
    for row in conn.execute("SELECT passcode_hash FROM admins"):
        if verify_passcode(passcode, row["passcode_hash"]):
            return True
    return False


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
