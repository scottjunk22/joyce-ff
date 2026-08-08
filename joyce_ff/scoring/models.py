"""
Stat-line data models for the scoring engine.

These are plain, validated containers for a single game's raw box-score
numbers. They deliberately mirror the granularity available from nflverse
weekly player stats + play-by-play, so the engine never has to guess.

Design rule: a stat line holds RAW COUNTS ONLY. No points, no thresholds,
no derived values. The engine turns these into points; nothing else does.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _nonneg(name: str, value: int | float) -> None:
    if value is None:
        raise ValueError(f"{name} must not be None (use 0 for a real zero)")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")


@dataclass(frozen=True)
class PlayerGame:
    """One individual player's raw stat line for one game (RB or R slot).

    Passing fields exist here only because some players (e.g. a WR throwing a
    trick-play TD) can accrue them; for the QB team-unit we use TeamUnitGame.
    """

    player: str = ""
    team: str = ""          # NFL team abbreviation
    week: int | None = None
    season: int | None = None

    rushing_yards: int = 0
    rushing_tds: int = 0

    receiving_yards: int = 0
    receptions: int = 0
    receiving_tds: int = 0

    # Special-teams / return scores that count as this player's 6-pt TD.
    return_tds: int = 0

    # Conversions credited to the scorer.
    two_point_conversions: int = 0

    # Present for completeness; individual passers are rare in R/RB slots.
    passing_yards: int = 0
    passing_tds: int = 0

    def __post_init__(self) -> None:
        for n in (
            "rushing_yards", "rushing_tds", "receiving_yards", "receptions",
            "receiving_tds", "return_tds", "two_point_conversions",
            "passing_yards", "passing_tds",
        ):
            _nonneg(n, getattr(self, n))


@dataclass(frozen=True)
class QBUnitGame:
    """A team's aggregated passing production for one game -> QB slot.

    'All pass yds go to QB': if a team uses two QBs, their passing yards and
    TDs aggregate here into the one slot.
    """

    team: str = ""
    week: int | None = None
    season: int | None = None

    passing_yards: int = 0     # team total
    passing_tds: int = 0       # team total
    # Only used if ASSUMPTIONS['QB_UNIT_GETS_RUSH_TD'] is enabled.
    qb_rushing_tds: int = 0

    def __post_init__(self) -> None:
        for n in ("passing_yards", "passing_tds", "qb_rushing_tds"):
            _nonneg(n, getattr(self, n))


@dataclass(frozen=True)
class KickerUnitGame:
    """A team's kicking production for one game -> K slot."""

    team: str = ""
    week: int | None = None
    season: int | None = None

    # Made field goals as a list of distances in yards, e.g. [23, 41, 52].
    field_goal_distances: tuple[int, ...] = ()
    extra_points_made: int = 0

    def __post_init__(self) -> None:
        _nonneg("extra_points_made", self.extra_points_made)
        for d in self.field_goal_distances:
            _nonneg("field_goal_distance", d)


@dataclass(frozen=True)
class CoachUnitGame:
    """A team's game result for one week -> C (Coach) slot."""

    team: str = ""
    week: int | None = None
    season: int | None = None

    won: bool = False
    tied: bool = False


@dataclass(frozen=True)
class DefenseUnitGame:
    """A team's defense + special-teams box score for one game -> DEF/ST slot."""

    team: str = ""
    week: int | None = None
    season: int | None = None

    points_allowed: int = 0
    yards_allowed: int = 0
    sacks: int = 0
    interceptions: int = 0
    fumble_recoveries: int = 0
    safeties: int = 0
    defensive_tds: int = 0        # INT/fumble return TDs
    special_teams_tds: int = 0    # kick/punt return TDs, blocked-kick TDs

    def __post_init__(self) -> None:
        for n in (
            "points_allowed", "yards_allowed", "sacks", "interceptions",
            "fumble_recoveries", "safeties", "defensive_tds",
            "special_teams_tds",
        ):
            _nonneg(n, getattr(self, n))


@dataclass
class ScoreBreakdown:
    """Auditable result: total plus an itemized list of (label, points).

    Every recommendation in this project must show its reasoning; scoring is
    where that starts. The breakdown lets us show exactly which rule paid.
    """

    total: float = 0.0
    items: list[tuple[str, float]] = field(default_factory=list)

    def add(self, label: str, points: float) -> None:
        if points:
            self.items.append((label, points))
            self.total += points

    def __str__(self) -> str:
        lines = [f"  {label}: {pts:g}" for label, pts in self.items]
        return f"Total: {self.total:g}\n" + "\n".join(lines)
