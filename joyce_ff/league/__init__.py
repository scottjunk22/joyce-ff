"""
League platform data layer — the server-side system of record for
stevejoyceff.com.

This is the shared, multi-user state (rosters, lineups, transactions, scores,
fees, elimination pool) that replaces the commissioner's by-hand process. The
scoring engine, projections, schedule, and draft modules feed into it; this
package owns persistence.

Money is stored in integer CENTS (fee_cents=200) to avoid float rounding.
Passcodes are stored only as hashes (see auth, later) — never plaintext.
"""

from .schema import connect, init_db, seed_reference

__all__ = ["connect", "init_db", "seed_reference"]
