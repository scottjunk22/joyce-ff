"""
Market-inefficiency report: where our threshold / 22-team board diverges from
standard-fantasy value, i.e. the edges to exploit on draft day.

Run: python manage.py market
"""

from __future__ import annotations

from joyce_ff.projections import history, market
from joyce_ff.projections import valuation as val


def _row(r):
    return (f"  {str(r['full_name'])[:22]:22s} {str(r['team']):>3} {r['position']:>3}  "
            f"our#{int(r['our_rank']):>3}  std#{int(r['std_rank']):>3}  "
            f"Δ{int(r['rank_delta']):>+4}   our {r['our_ppg']:>4.1f} / std {r['std_ppg']:>4.1f}  "
            f"[{r['why']}]")


def main(argv=None) -> int:
    print("Building standard-fantasy shadow valuation and diffing vs our board...")
    cmp = market.build_comparison()
    falls, over = market.edges(cmp)

    print("\n" + "=" * 74)
    print("STRUCTURAL EDGES  (asset classes standard fantasy misprices)")
    print("=" * 74)
    print("""  * DRAFT IS 11 TEAMS (your division), from the full NFL pool. Only ~11 of
    the ~32 of each team-unit get owned in your division -> TEAM UNITS ARE
    ABUNDANT (two-thirds available). So do NOT panic-draft QB/K/DEF/Coach for
    scarcity; there's always a good one left. The edge is their UNDERPRICED
    per-game value, not a scramble.
  * COACH — has NO standard-fantasy equivalent. 3 pts/win; a 14-win team's
    coach = 42 pts. Free, injury-proof points nobody else is even valuing.
  * KICKER — an afterthought in standard leagues, but worth ~8-11 pts/game
    here. Plenty available, so grab the underpriced value late rather than
    reaching early.
  * DEF/ST — same: real points, drafted late by habit, and not scarce.
  * TEAM-UNIT INJURY IMMUNITY — QB room / K / DEF / Coach carry NO individual
    injury risk (a backup scoring still counts for you). Preseason injury news
    that tanks a star's ADP elsewhere does NOT dent the unit's value here.
  * THE SCARCE ASSETS ARE THE INDIVIDUALS — with 33 RB and 44 receivers owned
    across your division, the RB/R pool is what actually thins out. Spend early
    capital there; let the abundant team units come to you.""")

    print("\n" + "=" * 74)
    print("VALUES THAT FALL TO US  (we rank them far above the standard room)")
    print("  our# = our rank in slot, std# = standard rank, Δ = std# − our# (higher = bigger steal)")
    print("=" * 74)
    for _, r in falls.iterrows():
        print(_row(r))

    print("\n" + "=" * 74)
    print("LET THEM HAVE THEM  (the room will overdraft these vs their value here)")
    print("=" * 74)
    for _, r in over.iterrows():
        print(_row(r))

    print("\n" + "=" * 74)
    print("DRAFT-DAY HEURISTICS")
    print("=" * 74)
    print("""  1. Fade PPR-volume receivers (lots of catches, few TDs): standard PPR
     inflates them; here receptions cap at 5 pts and yards are threshold'd.
  2. Prioritize TD-heavy / goal-line roles and low-bust floors — the board's
     Bust% and TD rate are what the ceiling-chasers ignore.
  3. Kicker/Coach/DEF are ABUNDANT in an 11-team division (two-thirds
     available) — don't reach early, but don't ignore their real injury-proof
     points either. Take the underpriced value in the middle-to-late rounds.
     Spend early picks on the genuinely scarce individual RB/R.
  4. TEs are TD-only dart throws in the R slot — never pay a 'TE premium'.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main([]))
