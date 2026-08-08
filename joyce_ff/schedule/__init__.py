"""
League schedule: the fixed matchup rotation (interleague + conference
round-robin) keyed by Team #, plus the FF-week -> NFL-week -> calendar mapping.

Matchups depend only on the Team # each manager draws on draft day; the NFL
schedule only sets the calendar dates. So we can show any team's full slate the
moment they know their Team #, using real NFL week dates from nflverse.
"""

from .rotation import (CONFERENCE_WEEKS, INTERLEAGUE_WEEKS, opponent,
                       team_schedule, validate_rotation)

__all__ = ["team_schedule", "opponent", "validate_rotation",
           "INTERLEAGUE_WEEKS", "CONFERENCE_WEEKS"]
