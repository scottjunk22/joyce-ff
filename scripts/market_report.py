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
    print("""  * COACH — has NO standard-fantasy equivalent. 3 pts/win; a 14-win team's
    coach = 42 pts. Nobody in the room has intuition for the coach cliff, and
    only ~32 exist for 22 teams. Draft it as a real scarce asset, not an
    afterthought.
  * KICKER — an afterthought in standard leagues (streamed, drafted last), but
    worth ~8-11 pts/game here and only ~32 exist. If the room waits on kickers,
    the top ones fall further than their value says they should.
  * DEF/ST — same behavioral gap: scarce team unit, drafted late by habit.
  * TEAM-UNIT INJURY IMMUNITY — QB room / K / DEF / Coach carry NO individual
    injury risk (a backup scoring still counts for you). Preseason injury news
    that tanks a star's ADP elsewhere does NOT dent the unit's value here.
  * 22-TEAM DEPTH — replacement level is far lower than standard's 12 teams, so
    'deep' players who are waiver fodder elsewhere are real starters here.""")

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
  3. Don't punt Kicker/Coach/DEF to the last rounds out of habit; they're
     scarce team units with real, injury-proof points. Time them to the cliff.
  4. TEs are TD-only dart throws in the R slot — never pay a 'TE premium'.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main([]))
