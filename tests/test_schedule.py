"""
Tests for the schedule rotation — validity of the transcribed 2025-26 table and
correct per-team lookups. No network (calendar dates are tested separately).
"""

import pytest

from joyce_ff.schedule import rotation as rot


def test_rotation_is_internally_valid():
    # Round-robin completeness, interleague permutations, reciprocity.
    rot.validate_rotation()


def test_interleague_lookup_matches_sheet():
    # 2025-26 sheet, FF Week 1: pair (R1, B5) -> Blue #5 plays Red #1.
    r = rot.opponent(5, "BLUE", 1)
    assert r["kind"] == "interleague"
    assert r["opp_conf"] == "RED" and r["opp_team"] == 1
    # and the reciprocal
    assert rot.opponent(1, "RED", 1)["opp_team"] == 5


def test_conference_lookup_and_bye():
    # FF Week 5: games include (4,7); Blue #4 plays Blue #7.
    r = rot.opponent(4, "BLUE", 5)
    assert r["kind"] == "conference" and r["opp_conf"] == "BLUE" and r["opp_team"] == 7
    # Team 11 is the FF Week 5 bye.
    assert rot.opponent(11, "BLUE", 5)["kind"] == "bye"


def test_full_slate_shape():
    sched = rot.team_schedule(4, "BLUE")
    assert len(sched) == 16                       # FF weeks 1-15 + week 16
    assert sched[-1]["kind"] == "no_play"
    kinds = [g["kind"] for g in sched[:15]]
    assert kinds[:4] == ["interleague"] * 4       # weeks 1-4 interleague
    assert sum(k == "bye" for k in kinds) == 1     # exactly one bye
    assert sum(k == "conference" for k in kinds) == 10


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        rot.opponent(12, "BLUE", 5)
    with pytest.raises(ValueError):
        rot.opponent(4, "GREEN", 5)
