"""
Joyce Fantasy Football — the rulebook, as data.

This module is the single source of truth for scoring. It contains ONLY
constants and pure tier tables — no logic. `engine.py` interprets these.

Everything here is transcribed from BRIEF.md and confirmed with the league
commissioner (Scott's dad). Where a rule is ambiguous, the ambiguity is
captured explicitly in `ASSUMPTIONS` below and gated behind a named flag so
the Phase-1 reconciliation against the site's posted scores can arbitrate it.

CRITICAL PRINCIPLE — threshold scoring:
    Yardage pays in STEP FUNCTIONS, not per yard. A 74-yard rushing game
    scores 0. A 75-yard game scores 2. Never average yardage and then score
    it; score each game against the ladder, then aggregate.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Touchdowns, kicking, conversions, coach
# ---------------------------------------------------------------------------

TD_ANY = 6            # any touchdown scored by a player (rush, catch, return)
TD_PASS_TO_PASSER = 3  # credited to the QB slot for throwing a TD
SAFETY = 2
TWO_POINT_CONVERSION = 2
EXTRA_POINT = 1        # made PAT kick -> K slot
EXTRA_POINT_PASS = 1   # "extra point pass" per rulebook (see ASSUMPTIONS #A5)
COACH_WIN = 3          # per NFL team win -> C (Coach) slot

# Field goals by distance (yards). (min_distance_inclusive, points)
# 1-39 -> 3, 40-49 -> 4, 50+ -> 5
FIELD_GOAL_TIERS = [
    (0, 3),    # 1-39 yds
    (40, 4),   # 40-49 yds
    (50, 5),   # 50+ yds
]

# ---------------------------------------------------------------------------
# Yardage ladders (threshold / step functions)
# ---------------------------------------------------------------------------
# Each ladder is an ordered list of (threshold_yards, points). Below the first
# threshold scores 0. Some ladders extend past their top explicit tier with a
# repeating "+STEP_POINTS per STEP_YARDS beyond STEP_FROM" rule.

# Rushing yards: 75=2, 100=3, 125=4, 150=5, 175=6, 200=7,
#                then +1 for every additional 25 yds beyond 200.
RUSHING_YARD_TIERS = [
    (75, 2),
    (100, 3),
    (125, 4),
    (150, 5),
    (175, 6),
    (200, 7),
]
RUSHING_YARD_EXTENSION = {"step_from": 200, "step_yards": 25, "step_points": 1}

# Receiving yards: identical ladder to rushing.
RECEIVING_YARD_TIERS = [
    (75, 2),
    (100, 3),
    (125, 4),
    (150, 5),
    (175, 6),
    (200, 7),
]
RECEIVING_YARD_EXTENSION = {"step_from": 200, "step_yards": 25, "step_points": 1}

# Passing yards: 250-299=3, 300-349=4, 350-399=5, 400-449=6, 450-499=7,
#                then +1 for every additional 50 yds beyond 450 (Q4 confirmed).
PASSING_YARD_TIERS = [
    (250, 3),
    (300, 4),
    (350, 5),
    (400, 6),
    (450, 7),
]
PASSING_YARD_EXTENSION = {"step_from": 450, "step_yards": 50, "step_points": 1}

# Receptions: 6-7=3, 8-9=4, 10+=5. No extension.
RECEPTION_TIERS = [
    (6, 3),
    (8, 4),
    (10, 5),
]
RECEPTION_EXTENSION = None

# ---------------------------------------------------------------------------
# Defense / Special Teams (the DEF/ST team-unit slot)
# ---------------------------------------------------------------------------
# No negative tiers anywhere: allowing 40 points simply earns nothing.

DEF_TD = 6              # defensive OR special-teams TD
DEF_SAFETY = 2
DEF_INTERCEPTION = 1
DEF_FUMBLE_RECOVERY = 1
DEF_SACK = 1

# Points allowed by the defense (team defense). Only the shutout-ish tier pays.
# (max_points_allowed_inclusive, points)
DEF_POINTS_ALLOWED_TIERS = [
    (9, 5),    # allow 0-9 pts -> 5
]

# Yards allowed by the defense. (max_yards_allowed_inclusive, points)
DEF_YARDS_ALLOWED_TIERS = [
    (199, 5),   # allow 0-199 yds -> 5
    (249, 3),   # allow 200-249 yds -> 3
]

# ---------------------------------------------------------------------------
# Roster / lineup structure (confirmed with commissioner)
# ---------------------------------------------------------------------------
# You DRAFT 11 assets and START 9 each week. The "bench" is just the flex you
# do not start (1 RB + 1 R). There is no separate bench and no IR.

TEAM_UNIT_SLOTS = ("C", "K", "DEF/ST", "QB")   # drafted as NFL team units
INDIVIDUAL_SLOTS = ("RB", "R")                  # drafted as individual players

DRAFTED_ROSTER = {
    "C": 1,       # Coach (team unit) — 3 pts per NFL team win
    "K": 1,       # Kicker (team unit)
    "DEF/ST": 1,  # Defense + Special Teams (team unit)
    "QB": 1,      # QB room (team unit) — all of a team's passing production
    "RB": 3,      # individual running backs
    "R": 4,       # individual receivers (WR + TE, any mix, no limits)
}  # total drafted = 11

WEEKLY_STARTERS = {
    "C": 1,
    "K": 1,
    "DEF/ST": 1,
    "QB": 1,
    "RB": 2,      # start 2 of 3
    "R": 3,       # start 3 of 4
}  # total started = 9

# League-wide counts, for replacement-level math (Phase 2 VOR).
NUM_TEAMS = 22
STARTED_RB_LEAGUEWIDE = NUM_TEAMS * WEEKLY_STARTERS["RB"]   # 44
STARTED_R_LEAGUEWIDE = NUM_TEAMS * WEEKLY_STARTERS["R"]     # 66
ROSTERED_RB_LEAGUEWIDE = NUM_TEAMS * DRAFTED_ROSTER["RB"]   # 66
ROSTERED_R_LEAGUEWIDE = NUM_TEAMS * DRAFTED_ROSTER["R"]     # 88

# ---------------------------------------------------------------------------
# ASSUMPTIONS — ambiguities not yet resolved by the commissioner.
# The Phase-1 reconciliation is designed to confirm or correct each of these.
# Each is a live toggle so we can flip it and re-run validation.
# ---------------------------------------------------------------------------

ASSUMPTIONS = {
    # A1: Does the QB team-unit slot receive 6 pts when the QB RUSHES for a TD?
    # The QB unit clearly gets passing yards + 3 per passing TD. Whether a
    # QB's rushing TD credits the QB slot (vs. crediting no one, since QBs are
    # not rostered individually) is unconfirmed. Default: NO.
    "QB_UNIT_GETS_RUSH_TD": False,

    # A2: Coach scoring on an NFL TIE (rare). Default: a tie is not a win -> 0.
    "COACH_TIE_IS_HALF_WIN": False,

    # A3: Points-allowed and yards-allowed are SEPARATE, additive awards for
    # the DEF/ST unit (a game can earn both). Default: True.
    "DEF_POINTS_AND_YARDS_ARE_ADDITIVE": True,

    # A4: A rostered individual's return TDs (kick/punt) count as his 6-pt TD,
    # AND the DEF/ST unit of that NFL team also gets 6 for the same play
    # (duplicate points across two owners — confirmed Q5). Default: True.
    "RETURN_TD_COUNTS_FOR_INDIVIDUAL": True,

    # A5: "Extra point pass = 1". Meaning unconfirmed (possibly a passer credit
    # on a PAT). Encoded literally as 1 pt; not yet attributed to a slot in the
    # engine. Flagged so validation can surface it. Default: ignore in scoring
    # until we see a discrepancy that needs it.
    "SCORE_EXTRA_POINT_PASS": False,
}
