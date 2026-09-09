"""Guards on the hand-typed champions list.

Team names are the join key between 36 years of history and the current
season's teams, so a one-character typo doesn't just look wrong — it silently
splits a team's record in two and can cost them a crown. This catches the next
one at commit time rather than on the front page.
"""

from __future__ import annotations

import difflib

from joyce_ff.league import titles
from joyce_ff.league.champions_seed import CHAMPIONS, rows

# Name pairs that LOOK like typos but haven't been confirmed either way by the
# commissioner. Listed here so the check stays useful instead of being switched
# off; resolve one by fixing the name, or move it down to CONFIRMED_DISTINCT.
UNRESOLVED = {("muddychickens", "muddychicks")}

# Pairs the commissioner has confirmed are genuinely different teams.
CONFIRMED_DISTINCT: set[tuple[str, str]] = set()


def _names():
    out = set()
    for _, champ, runner in CHAMPIONS:
        out.add(champ)
        if runner:
            out.add(runner)
    return out


def test_no_new_near_duplicate_team_names():
    keys = sorted({titles.key_for(n) for n in _names()})
    known = UNRESOLVED | CONFIRMED_DISTINCT
    found = set()
    for a in keys:
        for b in difflib.get_close_matches(a, keys, n=5, cutoff=0.86):
            if b != a:
                found.add(tuple(sorted((a, b))))
    assert not (found - known), (
        f"Near-identical team names in champions_seed.py: {sorted(found - known)}. "
        "Fix the typo, or add the pair to CONFIRMED_DISTINCT if they really are "
        "different teams.")


def test_every_season_maps_to_its_starting_year():
    """The commissioner's list is by ENDING year; we store starting years."""
    out = rows()
    assert len(out) == len(CHAMPIONS)
    for (end, champ, runner), (year, label, team, ru) in zip(sorted(CHAMPIONS), sorted(out)):
        assert year == end - 1
        assert label == f"{end - 1}-{str(end)[2:]}"
        assert team == champ and ru == runner
