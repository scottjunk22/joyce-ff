"""
Tests for defensive parsing of the hand-maintained league-site lineup tables,
and for the model tolerating real negative-yardage games.

The HTML fixture mirrors the site's quirks: a leading '#' column, seed/margin
noise in headers ('#4 OT Blitz +30'), a bare-number Total row, and shorthand
assets ('S-N', 'Seatt').
"""

import pytest

from joyce_ff.data_sources import league_site as ls
from joyce_ff.scoring.engine import score_player_game
from joyce_ff.scoring.models import PlayerGame

FIXTURE = """
<table>
  <tr><td>#</td><td>#5 Pike</td><td>#6 BGH</td></tr>
  <tr><td>C</td><td></td><td>NE 0</td></tr>
  <tr><td>K</td><td></td><td>NE 1</td></tr>
  <tr><td>DEF/ST</td><td></td><td>Seatt 15</td></tr>
  <tr><td>QB</td><td></td><td>Seatt 3</td></tr>
  <tr><td>RB</td><td></td><td>Walker 4</td></tr>
  <tr><td>R</td><td></td><td>Kupp 3</td></tr>
  <tr><td>Total</td><td></td><td>26</td></tr>
</table>
"""


def test_parser_extracts_filled_lineup_values():
    lineups = ls.parse_lineups(FIXTURE)
    assert len(lineups) == 1
    lu = lineups[0]
    slots = {(s.slot, s.asset): s.points for s in lu.slots}
    assert slots[("DEF/ST", "Seatt")] == 15
    assert slots[("QB", "Seatt")] == 3
    assert slots[("RB", "Walker")] == 4
    assert slots[("R", "Kupp")] == 3
    assert slots[("K", "NE")] == 1
    assert slots[("C", "NE")] == 0


def test_parser_captures_bare_total():
    lu = ls.parse_lineups(FIXTURE)[0]
    assert lu.posted_total == 26


def test_parser_ignores_empty_columns():
    # The empty 'Pike' column must not produce a phantom lineup.
    assert len(ls.parse_lineups(FIXTURE)) == 1


def test_header_team_cleaning():
    assert ls._parse_header_team("#4 OT Blitz +30") == "OT Blitz"
    assert ls._parse_header_team("#1 Barn Burners +14") == "Barn Burners"
    assert ls._parse_header_team("#") is None


def test_cell_parsing_handles_shorthand_and_negatives():
    assert ls._parse_cell("S-N 0") == ("S-N", 0.0)
    assert ls._parse_cell("Seatt 15") == ("Seatt", 15.0)
    assert ls._parse_cell("") is None


def test_model_tolerates_negative_receiving_yards():
    # A real completion behind the line for a loss -> engine scores 0, no crash.
    g = PlayerGame(player="x", receiving_yards=-1, receptions=1)
    assert score_player_game(g).total == 0
