"""
Tests for the draft-valuation math: shrinkage, replacement level, VOR.

These use tiny synthetic frames (no network / nflverse) so they run fast and
deterministically alongside the engine tests.
"""

import numpy as np
import pandas as pd

from joyce_ff.projections import valuation as val


def test_shrink_pulls_small_samples_toward_prior():
    # One "game" worth 6 pts, prior 1.5, k=6 -> (6 + 6*1.5)/(1+6) = 15/7 ~ 2.14
    got = val._shrink(pd.Series([6.0]), pd.Series([1.0]), prior=1.5, k=6.0).iloc[0]
    assert abs(got - (6.0 + 6 * 1.5) / (1 + 6)) < 1e-9
    assert got < 6.0  # a single big game does not project like a star


def test_shrink_barely_moves_full_sample():
    # 17 games averaging 8 pts (weighted num=136, den=17), prior 1.5, k=6
    got = val._shrink(pd.Series([136.0]), pd.Series([17.0]), prior=1.5, k=6.0).iloc[0]
    assert got > 6.0  # stays close to its real 8.0, only mildly regressed


def _fake_scored_players():
    # 3 players, all RB-eligible via the fake roster below.
    rows = []
    # star: 17 games @ ~9
    for wk in range(1, 18):
        rows.append((2025, wk, "STAR", "SF", 9, 9, 0, 0, 0, 0, 0, 0))
    # mid: 17 games @ ~3
    for wk in range(1, 18):
        rows.append((2025, wk, "MID", "GB", 3, 0, 0, 30, 3, 0, 0, 0))
    # onegame: 1 game @ 6
    rows.append((2025, 1, "ONE", "NYG", 6, 0, 1, 0, 0, 0, 0, 0))
    # replacement-level filler: 17 games @ ~1 (sets a low, realistic prior)
    for wk in range(1, 18):
        rows.append((2025, wk, "REPL", "CHI", 1, 0, 0, 0, 0, 0, 0, 0))
    cols = ["season", "week", "player_id", "team", "points",
            "rushing_yards", "rushing_tds", "receiving_yards", "receptions",
            "receiving_tds", "passing_yards", "return_tds"]
    return pd.DataFrame(rows, columns=cols)


def _fake_roster():
    return pd.DataFrame({
        "gsis_id": ["STAR", "MID", "ONE", "REPL"],
        "full_name": ["Star Back", "Mid Back", "One Gamer", "Repl Back"],
        "position": ["RB", "RB", "RB", "RB"],
        "team": ["SF", "GB", "NYG", "CHI"],
        "status": ["ACT", "ACT", "ACT", "ACT"],
    })


def test_player_board_orders_and_shrinks():
    board = val.player_board(_fake_scored_players(), _fake_roster())
    order = list(board["name"])
    assert order[0] == "Star Back"          # highest projection first
    assert order.index("Mid Back") < order.index("One Gamer")  # 1-game shrunk below steady mid
    # VOR is monotonic with projection within the slot
    sub = board[board["slot"] == "RB"].dropna(subset=["vor"])
    assert list(sub.sort_values("proj", ascending=False)["vor"]) == \
        sorted(sub["vor"], reverse=True)


def test_no_history_player_is_flagged_not_invented():
    sp = _fake_scored_players()
    roster = _fake_roster()
    roster.loc[len(roster)] = ["ROOKIE", "Rook Ie", "RB", "DAL", "ACT"]
    board = val.player_board(sp, roster)
    rk = board[board["name"] == "Rook Ie"].iloc[0]
    assert bool(rk["no_history"]) is True
    assert pd.isna(rk["proj"])              # never a fabricated number
