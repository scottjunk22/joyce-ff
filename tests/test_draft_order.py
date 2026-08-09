"""Tests for the draft pick order and its helpers."""

import pytest

from joyce_ff.draft import order


def test_order_is_valid():
    order.validate_order()


def test_sequence_covers_all_121_picks_in_order():
    seq = order.build_sequence()
    assert len(seq) == 121
    assert [s["overall"] for s in seq] == list(range(1, 122))
    # round 1 goes in straight slot order 1..11
    assert [s["slot"] for s in seq[:11]] == list(range(1, 12))
    # round 2 snakes back 11..1
    assert [s["slot"] for s in seq[11:22]] == list(range(11, 0, -1))


def test_slot_pick_overalls():
    # slot 1: round1 pick1 -> overall 1; round2 pick11 -> overall 22; etc.
    ov = order.slot_pick_overalls(1)
    assert len(ov) == 11
    assert ov[0] == 1 and ov[1] == 22


def test_picks_until_next():
    # slot 1 picks at overall 1, then 22. From pick 2, next is 22, gap 20.
    r = order.picks_until_next(1, 2)
    assert r["next_overall"] == 22 and r["gap"] == 20
    # exactly on your pick -> gap 0
    assert order.picks_until_next(1, 1)["gap"] == 0


def test_roster_template_is_eleven_spots():
    assert order.ROSTER_TEMPLATE.count("RB") == 3
    assert order.ROSTER_TEMPLATE.count("R") == 4
    assert len(order.ROSTER_TEMPLATE) == 11
    assert order.ROSTER_TEMPLATE[:4] == ["C", "K", "DEF/ST", "QB"]
