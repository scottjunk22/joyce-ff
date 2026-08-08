"""
Draft board: value-over-replacement boards for this league, computed entirely
from locally cached nflverse data (works offline after the first pull).
Projections are recency-weighted per-game engine points, shrunk for sample
size; VOR is vs the 11-team DIVISION replacement level (the draft is one
11-team division from the full NFL pool, separate pools per division).

Run: python manage.py board                 # top RB, R, and team units
     python manage.py board --slot RB --top 40
     python manage.py board --units          # team units only
"""

from __future__ import annotations

import sys

from joyce_ff.data_sources import nflverse as nv
from joyce_ff.projections import history, valuation as val

DRAFT_SEASON = 2026


def _fmt_player_row(i, r):
    flags = []
    if bool(r.get("no_history")):
        flags.append("NO-HISTORY")
    elif bool(r.get("low_sample")):
        flags.append("low-sample")
    flag = ("  " + ",".join(flags)) if flags else ""
    if r.get("proj") != r.get("proj"):  # NaN projection
        return (f"{i:>3} {'--':>4} {str(r['name'])[:22]:22s} {str(r.team):>3} "
                f"{r.position:>3}   (no NFL history — not projected){flag}")
    return (f"{i:>3} {int(r.tier):>4} {str(r['name'])[:22]:22s} {str(r.team):>3} "
            f"{r.position:>3} {r.proj:>5.2f} {r.vor:>5.2f} {r.p25:>5.1f} "
            f"{r.p75:>5.1f} {r.bust_rate*100:>3.0f}% {int(r.games_2025):>3}{flag}")


def _print_player_board(board, slot, top):
    sub = board[board["slot"] == slot].head(top)
    print(f"\n===== TOP {slot}  (proj pts/game, recency-weighted & shrunk; "
          f"VOR vs 11-team division replacement) =====")
    print(f"{'rk':>3} {'tier':>4} {'player':22s} {'tm':>3} {'pos':>3} "
          f"{'proj':>5} {'vor':>5} {'flr':>5} {'ceil':>5} {'bst':>4} {'g25':>3}")
    for i, (_, r) in enumerate(sub.iterrows(), 1):
        print(_fmt_player_row(i, r))
    repl = board[(board["slot"] == slot) & board["repl_level"].notna()]
    if not repl.empty:
        print(f"    replacement level: {repl['repl_level'].iloc[0]:.2f} pts/game")


def _print_unit_board(ub, unit, top):
    b = ub[unit].head(top)
    print(f"\n===== TOP {unit} team-units  (proj pts/game; VOR vs first unowned) =====")
    for i, (_, r) in enumerate(b.iterrows(), 1):
        print(f"{i:>3} {r.team:>3}  proj={r.proj:>5.2f}  vor={r.vor:>5.2f}  "
              f"floor={r.p25:>4.1f}  ceil={r.p75:>4.1f}  g25={int(r.games_2025)}")


def main(argv=None) -> int:
    argv = argv or []
    top = 40 if "--top" not in argv else int(argv[argv.index("--top") + 1])
    only_slot = argv[argv.index("--slot") + 1] if "--slot" in argv else None
    units_only = "--units" in argv

    print("Building boards from cached nflverse history (2023-2025)...")
    if not units_only:
        sp = history.scored_player_games()
        roster = nv.load_roster(DRAFT_SEASON)
        board = val.player_board(sp, roster)
        for slot in ([only_slot] if only_slot else ["RB", "R"]):
            _print_player_board(board, slot, top)

    if units_only or not only_slot:
        units = history.scored_team_unit_games()
        ub = val.team_unit_board(units)
        for unit in ["QB", "K", "DEF/ST", "C"]:
            _print_unit_board(ub, unit, 12)

    print("\n(Projections are a recent-form baseline, not gospel — floor/ceil "
          "and bust% show the risk. Rookies with no NFL history are flagged, "
          "never invented.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
