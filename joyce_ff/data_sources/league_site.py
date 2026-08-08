"""
League site fetch + defensive parse.

The site (https://joyce401.jimdofree.com/) is READ ONLY. We fetch it at most
once per run, record an immutable timestamped snapshot in SQLite, and parse
defensively — never guessing at a value we cannot read.

The homepage carries hand-maintained lineup tables: each fantasy team is a
column, each roster slot a row, and filled cells read like "Seatt 15" or
"Walker 4" (asset name + the points the commissioner assigned). Many columns
are blank. We extract only the columns that actually contain data.
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

HOME_URL = "https://joyce401.jimdofree.com/"
_UA = {"User-Agent": "joyce-ff local tool (polite single fetch; league owner's son)"}

SLOT_LABELS = {"C", "K", "DEF/ST", "QB", "RB", "R", "TOTAL"}


@dataclass
class LineupSlot:
    slot: str            # C, K, DEF/ST, QB, RB, R
    asset: str           # NFL team abbrev (unit) or player last-name (individual)
    points: float


@dataclass
class TeamLineup:
    team: str
    slots: list[LineupSlot] = field(default_factory=list)
    posted_total: float | None = None


def fetch_home(timeout: int = 60) -> str:
    """Fetch the homepage HTML. One polite request; caller records the snapshot."""
    req = urllib.request.Request(HOME_URL, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parse_header_team(text: str) -> str | None:
    """'#4 OT Blitz +30' -> 'OT Blitz'. Returns None if not a team header."""
    t = _clean(text)
    if not t or not t.startswith("#"):
        return None
    t = re.sub(r"^#\d*\s*", "", t)          # drop leading seed '#4 ' or bare '#'
    t = re.sub(r"\s*\+?\d+\s*$", "", t)      # drop trailing margin '+30' or '1'
    t = re.sub(r"draft order$", "", t, flags=re.I).strip()
    # A real team name must contain a letter (guards the '#' label column).
    return t if re.search(r"[A-Za-z]", t) else None


def _bare_number(text: str) -> float | None:
    t = _clean(text)
    return float(t) if re.fullmatch(r"-?\d+(?:\.\d+)?", t) else None


def _parse_cell(text: str) -> tuple[str, float] | None:
    """'Seatt 15' -> ('Seatt', 15.0); 'S-N 0' -> ('S-N', 0.0). None if no number."""
    t = _clean(text)
    if not t:
        return None
    m = re.match(r"^(.*\S)\s+(-?\d+(?:\.\d+)?)$", t)
    if not m:
        return None
    return m.group(1).strip(), float(m.group(2))


def parse_lineups(html: str) -> list[TeamLineup]:
    """Extract every lineup column that actually contains data.

    Fails loudly: if a row's slot label is unrecognized we skip it rather than
    guess. A team column is kept only if at least one slot cell had a number.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[TeamLineup] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = [_clean(td.get_text(" ", strip=True))
                        for td in rows[0].find_all(["td", "th"])]
        # Identify team columns (index -> team name). Column 0 is the slot label.
        team_cols = {i: _parse_header_team(h) for i, h in enumerate(header_cells)}
        team_cols = {i: t for i, t in team_cols.items() if t}
        if not team_cols:
            continue

        # Only treat this as a lineup table if the first column lists slots.
        first_col_labels = {_clean(r.find_all(["td", "th"])[0].get_text(" ", strip=True)).upper()
                            for r in rows[1:] if r.find_all(["td", "th"])}
        if not (first_col_labels & SLOT_LABELS):
            continue

        lineups = {i: TeamLineup(team=t) for i, t in team_cols.items()}
        for r in rows[1:]:
            cells = r.find_all(["td", "th"])
            if not cells:
                continue
            slot = _clean(cells[0].get_text(" ", strip=True)).upper()
            if slot not in SLOT_LABELS:
                continue
            if slot == "TOTAL":
                # The total cell is a bare number; the surrounding cell counts
                # are unreliable, so attach the single numeric found to whichever
                # column ends up holding this table's filled lineup.
                for i in team_cols:
                    if i < len(cells):
                        b = _bare_number(cells[i].get_text(" ", strip=True))
                        if b is not None:
                            lineups[i].posted_total = b
                continue
            for i in team_cols:
                if i >= len(cells):
                    continue
                parsed = _parse_cell(cells[i].get_text(" ", strip=True))
                if parsed is None:
                    continue
                asset, pts = parsed
                lineups[i].slots.append(LineupSlot(slot=slot, asset=asset, points=pts))

        for lu in lineups.values():
            if lu.slots:  # keep only columns with real lineup data
                results.append(lu)

    return results
