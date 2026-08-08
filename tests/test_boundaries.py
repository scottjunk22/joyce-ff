"""
Threshold-boundary tests — the ones the brief explicitly demands.

Every step edge is tested at (edge - 1), (edge), and a representative interior
value. Threshold scoring means an off-by-one here costs a real game, so these
are exhaustive on purpose.
"""

import pytest

from joyce_ff.scoring import engine


# ---------------------------------------------------------------------------
# Rushing yards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("yards,expected", [
    (0, 0), (1, 0), (74, 0),      # below first threshold
    (75, 2), (76, 2), (99, 2),    # 75-99 -> 2
    (100, 3), (124, 3),           # 100-124 -> 3
    (125, 4), (149, 4),           # 125-149 -> 4
    (150, 5), (174, 5),           # 150-174 -> 5
    (175, 6), (199, 6),           # 175-199 -> 6
    (200, 7), (224, 7),           # 200-224 -> 7
    (225, 8), (249, 8),           # +1 per 25 beyond 200
    (250, 9), (274, 9),
    (300, 11),                    # 7 + floor((300-200)/25) = 7 + 4
])
def test_rushing_yards(yards, expected):
    assert engine.rushing_yard_points(yards) == expected


def test_rushing_74_vs_75_is_the_key_cliff():
    assert engine.rushing_yard_points(74) == 0
    assert engine.rushing_yard_points(75) == 2


# ---------------------------------------------------------------------------
# Receiving yards (identical ladder)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("yards,expected", [
    (74, 0), (75, 2), (99, 2), (100, 3), (124, 3), (125, 4),
    (149, 4), (150, 5), (174, 5), (175, 6), (199, 6), (200, 7),
    (224, 7), (225, 8), (250, 9),
])
def test_receiving_yards(yards, expected):
    assert engine.receiving_yard_points(yards) == expected


def test_rushing_and_receiving_ladders_match():
    for y in range(0, 351):
        assert engine.rushing_yard_points(y) == engine.receiving_yard_points(y)


# ---------------------------------------------------------------------------
# Passing yards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("yards,expected", [
    (0, 0), (249, 0),             # below first threshold
    (250, 3), (299, 3),           # 250-299 -> 3
    (300, 4), (349, 4),           # 300-349 -> 4
    (350, 5), (399, 5),           # 350-399 -> 5
    (400, 6), (449, 6),           # 400-449 -> 6
    (450, 7), (499, 7),           # 450-499 -> 7
    (500, 8), (549, 8),           # +1 per 50 beyond 450 (Q4)
    (550, 9), (600, 10),
])
def test_passing_yards(yards, expected):
    assert engine.passing_yard_points(yards) == expected


def test_passing_249_vs_250_cliff():
    assert engine.passing_yard_points(249) == 0
    assert engine.passing_yard_points(250) == 3


def test_passing_500_equals_8_confirmed_q4():
    assert engine.passing_yard_points(500) == 8


# ---------------------------------------------------------------------------
# Receptions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rec,expected", [
    (0, 0), (5, 0),               # below first threshold
    (6, 3), (7, 3),               # 6-7 -> 3
    (8, 4), (9, 4),               # 8-9 -> 4
    (10, 5), (11, 5), (15, 5),    # 10+ -> 5 (no extension)
])
def test_receptions(rec, expected):
    assert engine.reception_points(rec) == expected


def test_receptions_5_vs_6_cliff():
    assert engine.reception_points(5) == 0
    assert engine.reception_points(6) == 3


# ---------------------------------------------------------------------------
# Field goals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dist,expected", [
    (18, 3), (39, 3),             # 1-39 -> 3
    (40, 4), (49, 4),             # 40-49 -> 4
    (50, 5), (55, 5), (63, 5),    # 50+ -> 5
])
def test_field_goals(dist, expected):
    assert engine.field_goal_points(dist) == expected


def test_field_goal_39_vs_40_and_49_vs_50():
    assert engine.field_goal_points(39) == 3
    assert engine.field_goal_points(40) == 4
    assert engine.field_goal_points(49) == 4
    assert engine.field_goal_points(50) == 5


# ---------------------------------------------------------------------------
# Defense: points-allowed and yards-allowed (descending "allow at most")
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pa,expected", [
    (0, 5), (9, 5),               # allow 0-9 -> 5
    (10, 0), (40, 0),             # no tier above 9 -> 0 (no negative tiers)
])
def test_points_allowed(pa, expected):
    assert engine.points_allowed_points(pa) == expected


@pytest.mark.parametrize("ya,expected", [
    (0, 5), (199, 5),             # allow 0-199 -> 5
    (200, 3), (249, 3),           # allow 200-249 -> 3
    (250, 0), (500, 0),           # above 249 -> 0
])
def test_yards_allowed(ya, expected):
    assert engine.yards_allowed_points(ya) == expected


def test_yards_allowed_199_vs_200_cliff():
    assert engine.yards_allowed_points(199) == 5
    assert engine.yards_allowed_points(200) == 3
