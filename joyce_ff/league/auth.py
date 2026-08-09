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
