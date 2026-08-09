"""
The draft pick order, from the card-draw table (confirmed & validated against
the commissioner's PDF, reduced from 12 to 11 slots).

DRAFT_ORDER[slot] = the pick number this slot holds in each of the 11 rounds
(slot = the Spades card you draw, 1-11). The order is a 4-round repeating
cycle. 11 teams x 11 rounds = 121 total picks.
"""

from __future__ import annotations

from ..scoring import rules

SLOTS = 11
ROUNDS = 11

# slot -> [pick in round 1, ..., pick in round 11]
DRAFT_ORDER: dict[int, list[int]] = {
    1:  [1, 11, 8, 4, 1, 11, 8, 4, 1, 11, 8],
    2:  [2, 10, 7, 5, 2, 10, 7, 5, 2, 10, 7],
    3:  [3, 9, 6, 6, 3, 9, 6, 6, 3, 9, 6],
    4:  [4, 8, 5, 7, 4, 8, 5, 7, 4, 8, 5],
    5:  [5, 7, 1, 11, 5, 7, 1, 11, 5, 7, 1],
    6:  [6, 6, 2, 10, 6, 6, 2, 10, 6, 6, 2],
    7:  [7, 5, 3, 9, 7, 5, 3, 9, 7, 5, 3],
    8:  [8, 4, 4, 8, 8, 4, 4, 8, 8, 4, 4],
    9:  [9, 3, 11, 1, 9, 3, 11, 1, 9, 3, 11],
    10: [10, 2, 10, 2, 10, 2, 10, 2, 10, 2, 10],
    11: [11, 1, 9, 3, 11, 1, 9, 3, 11, 1, 9],
}

# The 11 roster spots you fill, in a natural display order.
ROSTER_TEMPLATE: list[str] = (
    ["C", "K", "DEF/ST", "QB"]
    + ["RB"] * rules.DRAFTED_ROSTER["RB"]
    + ["R"] * rules.DRAFTED_ROSTER["R"]
)


def overall_pick(rnd: int, pick_in_round: int) -> int:
    return (rnd - 1) * SLOTS + pick_in_round


def build_sequence() -> list[dict]:
    """All 121 picks in draft order: {overall, round, pick, slot}."""
    seq = [None] * (SLOTS * ROUNDS)
    for slot, picks in DRAFT_ORDER.items():
        for r in range(1, ROUNDS + 1):
            p = picks[r - 1]
            ov = overall_pick(r, p)
            seq[ov - 1] = {"overall": ov, "round": r, "pick": p, "slot": slot}
    return seq


def slot_pick_overalls(slot: int) -> list[int]:
    """The overall pick numbers a given slot owns, ascending."""
    return sorted(overall_pick(r, DRAFT_ORDER[slot][r - 1])
                  for r in range(1, ROUNDS + 1))


def picks_until_next(slot: int, current_overall: int) -> dict | None:
    """From current overall pick, when is `slot` next up?

    Returns {"next_overall", "gap"} where gap is how many picks (including the
    current one if it's not yet made) until this slot picks. None if the slot
    has no pick at/after current_overall.
    """
    for ov in slot_pick_overalls(slot):
        if ov >= current_overall:
            return {"next_overall": ov, "gap": ov - current_overall}
    return None


def validate_order() -> None:
    """Assert the draft order is well-formed: every round is a permutation of
    1..11, and every slot picks exactly once per round."""
    for r in range(1, ROUNDS + 1):
        picks = sorted(DRAFT_ORDER[s][r - 1] for s in range(1, SLOTS + 1))
        assert picks == list(range(1, SLOTS + 1)), \
            f"round {r} not a permutation of 1-{SLOTS}: {picks}"
    seq = build_sequence()
    assert all(x is not None for x in seq), "gap in pick sequence"
    assert len({(s["overall"]) for s in seq}) == SLOTS * ROUNDS, "dup overall pick"
