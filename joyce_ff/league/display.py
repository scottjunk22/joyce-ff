"""
How NFL teams are shown to people.

Stored team codes follow the NFL schedule's spelling (nflverse), because every
join — schedule, byes, stats, locks — depends on it. That spelling calls the
Rams "LA", which next to the Chargers' "LAC" reads as either Los Angeles team.
People see "LAR" instead.

Display only: never write these back, and never send them where the site
expects a code (a lineup, a trade, a draft pick).
"""

from __future__ import annotations

SHOWN = {"LA": "LAR"}


def team(abbr):
    """The code a person sees for a stored NFL team code."""
    return SHOWN.get(abbr, abbr) if abbr else abbr


def unit(abbr, unit_type) -> str:
    """A team unit's label, e.g. "LAR QB"."""
    return f"{team(abbr)} {unit_type or ''}".strip()
