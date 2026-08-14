"""
Per-slot scoring tests: full stat lines -> total points + breakdown.

These verify that categories combine correctly and that the flagged
ASSUMPTIONS behave as documented (and can be toggled).
"""

import copy

import pytest

from joyce_ff.scoring import rules
from joyce_ff.scoring.engine import (
    score_coach_unit_game,
    score_defense_unit_game,
    score_kicker_unit_game,
    score_player_game,
    score_qb_unit_game,
)
from joyce_ff.scoring.models import (
    CoachUnitGame,
    DefenseUnitGame,
    KickerUnitGame,
    PlayerGame,
    QBUnitGame,
)


# ---------------------------------------------------------------------------
# Individual player (RB / R)
# ---------------------------------------------------------------------------

def test_player_rushing_game_with_td():
    # 110 rush yds (3) + 1 rush TD (6) = 9
    g = PlayerGame(player="Walker", team="SEA", rushing_yards=110, rushing_tds=1)
    b = score_player_game(g)
    assert b.total == 9
    assert ("110 rush yds", 3) in b.items
    assert ("1 TD", 6) in b.items


def test_player_receiving_game_full_line():
    # 128 rec yds (4) + 9 rec (4) + 1 rec TD (6) = 14
    g = PlayerGame(player="Kupp", team="LA", receiving_yards=128, receptions=9, receiving_tds=1)
    b = score_player_game(g)
    assert b.total == 14


def test_player_sub_threshold_scores_zero_yardage():
    # 74 rush yds -> 0, 5 catches -> 0, no TD -> total 0
    g = PlayerGame(player="scrub", rushing_yards=74, receptions=5)
    assert score_player_game(g).total == 0


def test_player_dual_rush_receive_thresholds_do_not_combine():
    # Thresholds are per-category: 60 rush (0) + 60 rec (0) = 0, NOT 120 -> pts.
    g = PlayerGame(player="flex", rushing_yards=60, receiving_yards=60)
    assert score_player_game(g).total == 0


def test_player_two_point_conversion():
    g = PlayerGame(player="x", receiving_yards=80, receptions=6, two_point_conversions=1)
    # 80 rec (2) + 6 rec (3) + 2pt (2) = 7
    assert score_player_game(g).total == 7


def test_return_td_counts_for_individual_by_default():
    g = PlayerGame(player="Shaheed", team="NO", return_tds=1)
    assert score_player_game(g).total == 6


def test_return_td_can_be_toggled_off():
    a = copy.deepcopy(rules.ASSUMPTIONS)
    a["RETURN_TD_COUNTS_FOR_INDIVIDUAL"] = False
    g = PlayerGame(player="Shaheed", team="NO", return_tds=1)
    assert score_player_game(g, assumptions=a).total == 0


# ---------------------------------------------------------------------------
# QB unit
# ---------------------------------------------------------------------------

def test_qb_unit_passing_yards_and_tds():
    # 312 pass yds (4) + 2 TD passes (2*3=6) = 10
    g = QBUnitGame(team="SEA", passing_yards=312, passing_tds=2)
    assert score_qb_unit_game(g).total == 10


def test_qb_unit_aggregates_two_passers_conceptually():
    # Whoever populates the model sums both QBs; engine just scores the total.
    g = QBUnitGame(team="CLE", passing_yards=260, passing_tds=1)
    assert score_qb_unit_game(g).total == 3 + 3  # 260 -> 3, one TD -> 3


def test_qb_rush_td_off_by_default_on_by_flag():
    g = QBUnitGame(team="BAL", passing_yards=200, passing_tds=0, qb_rushing_tds=1)
    assert score_qb_unit_game(g).total == 0  # 200 pass yds < 250, flag off
    a = copy.deepcopy(rules.ASSUMPTIONS)
    a["QB_UNIT_GETS_RUSH_TD"] = True
    assert score_qb_unit_game(g, assumptions=a).total == 6


# ---------------------------------------------------------------------------
# Kicker unit
# ---------------------------------------------------------------------------

def test_kicker_unit_mixed_field_goals_and_xp():
    # FG 23 (3) + FG 45 (4) + FG 52 (5) + 3 XP (3) = 15
    g = KickerUnitGame(team="NE", field_goal_distances=(23, 45, 52), extra_points_made=3)
    assert score_kicker_unit_game(g).total == 15


def test_kicker_unit_no_kicks():
    assert score_kicker_unit_game(KickerUnitGame(team="NE")).total == 0


# ---------------------------------------------------------------------------
# Coach unit
# ---------------------------------------------------------------------------

def test_coach_win_is_three():
    assert score_coach_unit_game(CoachUnitGame(team="KC", won=True)).total == 3


def test_coach_loss_is_zero():
    assert score_coach_unit_game(CoachUnitGame(team="KC", won=False)).total == 0


def test_coach_tie_default_zero_but_half_with_flag():
    g = CoachUnitGame(team="KC", tied=True)
    assert score_coach_unit_game(g).total == 0
    a = copy.deepcopy(rules.ASSUMPTIONS)
    a["COACH_TIE_IS_HALF_WIN"] = True
    assert score_coach_unit_game(g, assumptions=a).total == 1.5


# ---------------------------------------------------------------------------
# Defense / ST unit
# ---------------------------------------------------------------------------

def test_defense_shutout_ish_game():
    # allow 7 pts (5) + allow 180 yds (5) + 3 sacks (3) + 2 INT (2) = 15
    g = DefenseUnitGame(team="SF", points_allowed=7, yards_allowed=180, sacks=3, interceptions=2)
    assert score_defense_unit_game(g).total == 15


def test_defense_points_and_yards_additive_by_default():
    g = DefenseUnitGame(team="SF", points_allowed=9, yards_allowed=199)
    assert score_defense_unit_game(g).total == 10  # 5 + 5


def test_defense_points_and_yards_max_when_flag_off():
    a = copy.deepcopy(rules.ASSUMPTIONS)
    a["DEF_POINTS_AND_YARDS_ARE_ADDITIVE"] = False
    g = DefenseUnitGame(team="SF", points_allowed=9, yards_allowed=199)
    assert score_defense_unit_game(g, assumptions=a).total == 5  # max(5,5)


def test_defense_blowout_allowed_earns_nothing_but_not_negative():
    # allow 45 pts, 480 yds -> 0 from tiers, but still gets sack/INT credit
    g = DefenseUnitGame(team="X", points_allowed=45, yards_allowed=480, sacks=1)
    assert score_defense_unit_game(g).total == 1


def test_defense_special_teams_td():
    g = DefenseUnitGame(team="NO", points_allowed=24, yards_allowed=350, special_teams_tds=1)
    assert score_defense_unit_game(g).total == 6


def test_defense_safety_and_defensive_td():
    g = DefenseUnitGame(team="X", points_allowed=14, yards_allowed=300, safeties=1, defensive_tds=1)
    assert score_defense_unit_game(g).total == 8  # 2 + 6
