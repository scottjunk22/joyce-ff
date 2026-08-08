"""
The fixed matchup rotation, transcribed from the league's 2025-26 schedule
(the "11 Team Round Robin" sheet) and validated programmatically.

- FF Weeks 1-4  : INTERLEAGUE (opposite conference). Pairs are (RED#, BLUE#).
- FF Weeks 5-15 : CONFERENCE round-robin, run identically inside BOTH Blue and
                  Red. Each week: 5 games + 1 bye (11 teams).
- FF Week 16    : No Play (NFL Week 18).

Team numbers are 1-11 within a conference. This pattern is reused each season;
only the calendar (which NFL week each FF week falls on) changes.
"""

from __future__ import annotations

BLUE, RED = "BLUE", "RED"
NUM_TEAMS_PER_CONF = 11

INTERLEAGUE_WEEKS = range(1, 5)
CONFERENCE_WEEKS = range(5, 16)
NO_PLAY_WEEK = 16

# Interleague pairs as (RED#, BLUE#), by FF week.
INTERLEAGUE: dict[int, list[tuple[int, int]]] = {
    1: [(1, 5), (2, 6), (3, 7), (4, 8), (5, 9), (6, 10), (7, 11),
        (8, 1), (9, 2), (10, 3), (11, 4)],
    2: [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8),
        (8, 9), (9, 10), (10, 11), (11, 1)],
    3: [(1, 3), (2, 4), (3, 5), (4, 6), (5, 7), (6, 8), (7, 9),
        (8, 10), (9, 11), (10, 1), (11, 2)],
    4: [(1, 4), (2, 5), (3, 6), (4, 7), (5, 8), (6, 9), (7, 10),
        (8, 11), (9, 1), (10, 2), (11, 3)],
}

# Conference round-robin, by FF week -> {"games": [(a, b), ...], "bye": team}.
# Same pairings apply within Blue and within Red.
CONFERENCE: dict[int, dict] = {
    5:  {"games": [(1, 10), (2, 9), (3, 8), (4, 7), (5, 6)], "bye": 11},
    6:  {"games": [(6, 4), (7, 3), (8, 2), (9, 1), (10, 11)], "bye": 5},
    7:  {"games": [(2, 11), (3, 10), (4, 9), (5, 8), (6, 7)], "bye": 1},
    8:  {"games": [(7, 5), (8, 4), (9, 3), (10, 2), (11, 1)], "bye": 6},
    9:  {"games": [(3, 1), (4, 11), (5, 10), (6, 9), (7, 8)], "bye": 2},
    10: {"games": [(8, 6), (9, 5), (10, 4), (11, 3), (1, 2)], "bye": 7},
    11: {"games": [(4, 2), (5, 1), (6, 11), (7, 10), (8, 9)], "bye": 3},
    12: {"games": [(9, 7), (10, 6), (11, 5), (1, 4), (2, 3)], "bye": 8},
    13: {"games": [(5, 3), (6, 2), (7, 1), (8, 11), (9, 10)], "bye": 4},
    14: {"games": [(10, 8), (11, 7), (1, 6), (2, 5), (3, 4)], "bye": 9},
    15: {"games": [(11, 9), (1, 8), (2, 7), (3, 6), (4, 5)], "bye": 10},
}


def _other_conf(conf: str) -> str:
    return RED if conf == BLUE else BLUE


def opponent(team: int, conf: str, ff_week: int) -> dict:
    """Return this team's matchup in a given FF week.

    dict: {"ff_week", "kind": interleague|conference|no_play|bye,
           "opp_conf", "opp_team"} — opp_team is None for bye/no_play.
    """
    conf = conf.upper()
    if conf not in (BLUE, RED):
        raise ValueError(f"conf must be BLUE or RED, got {conf!r}")
    if not (1 <= team <= NUM_TEAMS_PER_CONF):
        raise ValueError(f"team must be 1-{NUM_TEAMS_PER_CONF}, got {team}")

    if ff_week == NO_PLAY_WEEK:
        return {"ff_week": ff_week, "kind": "no_play", "opp_conf": None, "opp_team": None}

    if ff_week in INTERLEAGUE:
        for red, blue in INTERLEAGUE[ff_week]:
            mine, theirs = (blue, red) if conf == BLUE else (red, blue)
            if mine == team:
                return {"ff_week": ff_week, "kind": "interleague",
                        "opp_conf": _other_conf(conf), "opp_team": theirs}
        raise ValueError(f"team {team} not found in interleague week {ff_week}")

    if ff_week in CONFERENCE:
        rd = CONFERENCE[ff_week]
        if rd["bye"] == team:
            return {"ff_week": ff_week, "kind": "bye", "opp_conf": None, "opp_team": None}
        for a, b in rd["games"]:
            if team in (a, b):
                return {"ff_week": ff_week, "kind": "conference",
                        "opp_conf": conf, "opp_team": b if a == team else a}
        raise ValueError(f"team {team} not found in conference week {ff_week}")

    raise ValueError(f"invalid FF week {ff_week}")


def team_schedule(team: int, conf: str) -> list[dict]:
    """Full 15-game slate (FF weeks 1-15) for a team, plus the week-16 no-play."""
    return [opponent(team, conf, wk) for wk in range(1, NO_PLAY_WEEK + 1)]


def validate_rotation() -> None:
    """Assert the transcribed rotation is internally valid. Raises on any flaw.

    - each interleague week pairs every RED and every BLUE team exactly once
    - conference round-robin: each team meets all 10 others exactly once and
      byes exactly once across weeks 5-15
    - matchups are reciprocal
    """
    teams = set(range(1, NUM_TEAMS_PER_CONF + 1))

    for wk, pairs in INTERLEAGUE.items():
        reds = [r for r, _ in pairs]
        blues = [b for _, b in pairs]
        assert sorted(reds) == sorted(teams), f"interleague wk{wk} red not a permutation"
        assert sorted(blues) == sorted(teams), f"interleague wk{wk} blue not a permutation"

    for conf in (BLUE, RED):
        for t in teams:
            opps, byes = [], 0
            for wk in CONFERENCE_WEEKS:
                r = opponent(t, conf, wk)
                if r["kind"] == "bye":
                    byes += 1
                else:
                    opps.append(r["opp_team"])
            assert byes == 1, f"{conf} team {t} has {byes} byes"
            assert sorted(opps) == sorted(teams - {t}), \
                f"{conf} team {t} conference opponents wrong: {sorted(opps)}"

    # reciprocity across all weeks
    for conf in (BLUE, RED):
        for t in teams:
            for wk in range(1, NO_PLAY_WEEK):
                r = opponent(t, conf, wk)
                if r["opp_team"] is None:
                    continue
                back = opponent(r["opp_team"], r["opp_conf"], wk)
                assert back["opp_team"] == t and back["opp_conf"] == conf, \
                    f"non-reciprocal: {conf}{t} wk{wk} -> {r}"
