"""
Tests for the `tiered_score` primitive and a few real-world scenarios,
including the 'duplicate points across two owners' case from Q5.
"""

import pytest

from joyce_ff.scoring import engine
from joyce_ff.scoring.engine import score_defense_unit_game, score_player_game
from joyce_ff.scoring.models import DefenseUnitGame, PlayerGame


def test_tiered_score_below_first_threshold_is_zero():
    assert engine.tiered_score(0, [(75, 2), (100, 3)]) == 0
    assert engine.tiered_score(74, [(75, 2), (100, 3)]) == 0


def test_tiered_score_picks_highest_matching_tier():
    tiers = [(75, 2), (100, 3), (125, 4)]
    assert engine.tiered_score(130, tiers) == 4
    assert engine.tiered_score(100, tiers) == 3


def test_tiered_score_extension_adds_beyond_top_tier():
    tiers = [(200, 7)]
    ext = {"step_from": 200, "step_yards": 25, "step_points": 1}
    assert engine.tiered_score(200, tiers, ext) == 7
    assert engine.tiered_score(224, tiers, ext) == 7
    assert engine.tiered_score(225, tiers, ext) == 8
    assert engine.tiered_score(250, tiers, ext) == 9


def test_tiered_score_empty_tiers_raises():
    with pytest.raises(ValueError):
        engine.tiered_score(100, [])


def test_duplicate_points_scenario_q5():
    """A Saints kickoff-return TD by Shaheed pays BOTH owners.

    - The owner of Shaheed (as an R) gets 6 for his return TD.
    - The owner of the NO DEF/ST unit gets 6 for the special-teams TD.
    Same play, two different rosters, two separate 6-point credits.
    """
    shaheed = PlayerGame(player="Shaheed", team="NO", return_tds=1)
    no_def = DefenseUnitGame(
        team="NO", points_allowed=20, yards_allowed=310, special_teams_tds=1
    )
    assert score_player_game(shaheed).total == 6
    assert score_defense_unit_game(no_def).total == 6


def test_consistency_beats_volatility_illustration():
    """The brief's core insight, encoded as a test.

    A back who runs for 80 every week scores 2/game. A back alternating
    40 and 120 averages the same yardage but scores (0 + 3)/2 = 1.5/game.
    Boom-bust is strictly worse under threshold scoring.
    """
    steady = [engine.rushing_yard_points(80) for _ in range(2)]
    volatile = [engine.rushing_yard_points(40), engine.rushing_yard_points(120)]
    assert sum(steady) / 2 == 2.0
    assert sum(volatile) / 2 == 1.5
    assert sum(steady) > sum(volatile)
