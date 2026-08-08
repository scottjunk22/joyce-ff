"""
The scoring engine.

Pure functions that turn raw stat lines (models.py) into points using the
rulebook (rules.py). No I/O, no randomness, no external dependencies — this
module must be trivially unit-testable and identical on every machine.

Two layers:
  1. `tiered_score` — the one primitive behind every yardage/reception ladder.
  2. category scorers + per-slot scorers that assemble a ScoreBreakdown.
"""

from __future__ import annotations

from math import floor

from . import rules
from .models import (
    CoachUnitGame,
    DefenseUnitGame,
    KickerUnitGame,
    PlayerGame,
    QBUnitGame,
    ScoreBreakdown,
)


# ---------------------------------------------------------------------------
# The primitive: a step-function score with an optional repeating extension.
# ---------------------------------------------------------------------------

def tiered_score(
    value: float,
    tiers: list[tuple[int, int]],
    extension: dict | None = None,
) -> int:
    """Score `value` against an ascending list of (threshold, points) tiers.

    - Below the first threshold -> 0.
    - Otherwise, the points of the highest tier whose threshold <= value.
    - If `extension` is given ({step_from, step_yards, step_points}) and
      value >= step_from, add step_points for every full step_yards beyond
      step_from. The top explicit tier's threshold is expected to equal
      step_from, so there is no double counting at the boundary.

    Integer thresholds; `value` may be int or float (we compare directly).
    """
    if not tiers:
        raise ValueError("tiers must be non-empty")

    points = 0
    for threshold, tier_points in tiers:
        if value >= threshold:
            points = tier_points
        else:
            break

    if extension is not None and value >= extension["step_from"]:
        steps = floor((value - extension["step_from"]) / extension["step_yards"])
        points += steps * extension["step_points"]

    return points


# ---------------------------------------------------------------------------
# Category scorers (raw stat -> points). These are the audited building blocks.
# ---------------------------------------------------------------------------

def rushing_yard_points(yards: float) -> int:
    return tiered_score(yards, rules.RUSHING_YARD_TIERS, rules.RUSHING_YARD_EXTENSION)


def receiving_yard_points(yards: float) -> int:
    return tiered_score(yards, rules.RECEIVING_YARD_TIERS, rules.RECEIVING_YARD_EXTENSION)


def passing_yard_points(yards: float) -> int:
    return tiered_score(yards, rules.PASSING_YARD_TIERS, rules.PASSING_YARD_EXTENSION)


def reception_points(receptions: int) -> int:
    return tiered_score(receptions, rules.RECEPTION_TIERS, rules.RECEPTION_EXTENSION)


def field_goal_points(distance: int) -> int:
    """Points for a single made field goal of `distance` yards."""
    return tiered_score(distance, rules.FIELD_GOAL_TIERS, None)


def points_allowed_points(points_allowed: int) -> int:
    return tiered_score_desc(points_allowed, rules.DEF_POINTS_ALLOWED_TIERS)


def yards_allowed_points(yards_allowed: int) -> int:
    return tiered_score_desc(yards_allowed, rules.DEF_YARDS_ALLOWED_TIERS)


def tiered_score_desc(value: float, tiers: list[tuple[int, int]]) -> int:
    """Descending 'allow at most N -> points' scoring (defense).

    tiers is ascending by max-threshold, e.g. [(9, 5)] means 'allow 0-9 -> 5'.
    For yards: [(199, 5), (249, 3)] -> the FIRST (lowest) matching cap wins,
    because allowing fewer yards should never pay less.
    """
    if not tiers:
        raise ValueError("tiers must be non-empty")
    for cap, pts in tiers:  # ascending caps; first cap we fit under wins
        if value <= cap:
            return pts
    return 0


# ---------------------------------------------------------------------------
# Per-slot scorers -> ScoreBreakdown (auditable).
# ---------------------------------------------------------------------------

def score_player_game(g: PlayerGame, assumptions: dict | None = None) -> ScoreBreakdown:
    """Score an individual player (RB or R slot) for one game."""
    a = assumptions if assumptions is not None else rules.ASSUMPTIONS
    b = ScoreBreakdown()

    b.add("rushing yards", rushing_yard_points(g.rushing_yards))
    b.add("receiving yards", receiving_yard_points(g.receiving_yards))
    b.add("receptions", reception_points(g.receptions))

    # Any TD the player scores is 6.
    td_count = g.rushing_tds + g.receiving_tds
    if a.get("RETURN_TD_COUNTS_FOR_INDIVIDUAL", True):
        td_count += g.return_tds
    b.add("touchdowns", td_count * rules.TD_ANY)

    b.add("2-pt conversions", g.two_point_conversions * rules.TWO_POINT_CONVERSION)

    # A player who threw a TD (trick play) earns the passer credit too.
    if g.passing_tds:
        b.add("TD passes thrown", g.passing_tds * rules.TD_PASS_TO_PASSER)
    if g.passing_yards:
        b.add("passing yards", passing_yard_points(g.passing_yards))

    return b


def score_qb_unit_game(g: QBUnitGame, assumptions: dict | None = None) -> ScoreBreakdown:
    """Score a team's QB unit for one game (all passing production aggregates)."""
    a = assumptions if assumptions is not None else rules.ASSUMPTIONS
    b = ScoreBreakdown()

    b.add("passing yards", passing_yard_points(g.passing_yards))
    b.add("TD passes", g.passing_tds * rules.TD_PASS_TO_PASSER)

    if a.get("QB_UNIT_GETS_RUSH_TD", False):
        b.add("QB rushing TDs", g.qb_rushing_tds * rules.TD_ANY)

    return b


def score_kicker_unit_game(g: KickerUnitGame, assumptions: dict | None = None) -> ScoreBreakdown:
    """Score a team's kicking unit for one game."""
    b = ScoreBreakdown()
    for dist in g.field_goal_distances:
        b.add(f"FG {dist}yd", field_goal_points(dist))
    b.add("extra points", g.extra_points_made * rules.EXTRA_POINT)
    return b


def score_coach_unit_game(g: CoachUnitGame, assumptions: dict | None = None) -> ScoreBreakdown:
    """Score a coach unit for one game: 3 pts per win."""
    a = assumptions if assumptions is not None else rules.ASSUMPTIONS
    b = ScoreBreakdown()
    if g.won:
        b.add("coach win", rules.COACH_WIN)
    elif g.tied and a.get("COACH_TIE_IS_HALF_WIN", False):
        b.add("coach tie", rules.COACH_WIN / 2)
    return b


def score_defense_unit_game(g: DefenseUnitGame, assumptions: dict | None = None) -> ScoreBreakdown:
    """Score a team's DEF/ST unit for one game."""
    a = assumptions if assumptions is not None else rules.ASSUMPTIONS
    b = ScoreBreakdown()

    b.add("defensive TDs", g.defensive_tds * rules.DEF_TD)
    b.add("special-teams TDs", g.special_teams_tds * rules.DEF_TD)
    b.add("safeties", g.safeties * rules.DEF_SAFETY)
    b.add("interceptions", g.interceptions * rules.DEF_INTERCEPTION)
    b.add("fumble recoveries", g.fumble_recoveries * rules.DEF_FUMBLE_RECOVERY)
    b.add("sacks", g.sacks * rules.DEF_SACK)

    pa = points_allowed_points(g.points_allowed)
    ya = yards_allowed_points(g.yards_allowed)
    if a.get("DEF_POINTS_AND_YARDS_ARE_ADDITIVE", True):
        b.add("points allowed", pa)
        b.add("yards allowed", ya)
    else:
        b.add("points/yards allowed (max)", max(pa, ya))

    return b
