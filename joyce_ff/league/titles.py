"""
Championship history, attached to the teams that are playing right now.

The `champions` table holds 36 years of hand-typed team names; the current
season's names are typed fresh each August. Joining the two is a name match,
and name matching is where a feature like this quietly goes wrong — so the
rule here is deliberately conservative:

  * squash case, spacing and punctuation ("Shuffling Crew" == "ShufflingCrew");
  * drop a trailing parenthetical, which is how managers get written down
    ("Ribears (Joe)" == "Ribears");
  * consult ALIASES for the one-off seasons a manager ran under a different
    name, which only the commissioner can tell us about;
  * and when nothing matches, show NOTHING.

A missing crown is a small omission the commissioner can report. A wrong one
rewrites league history on the front page, so we never guess.
"""

from __future__ import annotations

import re
import sqlite3

# One-off name changes, as reported by the commissioner: every key is an
# alternate spelling, the value is the name that team is normally known by.
# Both sides are written the way they appear on the site; they're normalised
# below, so case and spacing here don't matter.
#
#     "Barn Brners": "Barn Burners",
#
ALIASES: dict[str, str] = {}

_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def norm(name: str | None) -> str:
    """The comparison form of a team name. Not for display — ever."""
    if not name:
        return ""
    return "".join(ch for ch in _PAREN.sub("", name).lower() if ch.isalnum())


_ALIAS_N = None


def _alias(key: str) -> str:
    global _ALIAS_N
    if _ALIAS_N is None:
        _ALIAS_N = {norm(k): norm(v) for k, v in ALIASES.items()}
    return _ALIAS_N.get(key, key)


def key_for(name: str | None) -> str:
    return _alias(norm(name))


def titles_by_key(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """{comparison key: [season labels, newest first]} across all of history."""
    out: dict[str, list[str]] = {}
    for r in conn.execute("SELECT label, team FROM champions ORDER BY year DESC"):
        out.setdefault(key_for(r["team"]), []).append(r["label"])
    return out


def defending_key(conn: sqlite3.Connection) -> str | None:
    """The reigning champion's key — the crown that flies this season."""
    r = conn.execute("SELECT team FROM champions ORDER BY year DESC LIMIT 1").fetchone()
    return key_for(r["team"]) if r else None


def for_season(conn: sqlite3.Connection, season_id: int) -> dict[int, dict]:
    """{team_id: {"years": [...], "count": n, "defending": bool}} for the teams
    in one season. Teams with no titles are omitted entirely, so the caller can
    treat "absent" and "never won" as the same thing."""
    try:
        hist = titles_by_key(conn)
        holder = defending_key(conn)
    except sqlite3.OperationalError:      # champions table predates this DB
        return {}
    out: dict[int, dict] = {}
    for t in conn.execute("SELECT id, name FROM teams WHERE season_id=?", (season_id,)):
        k = key_for(t["name"])
        years = hist.get(k)
        if not years:
            continue
        out[t["id"]] = {"years": years, "count": len(years), "defending": k == holder}
    return out
