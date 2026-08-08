# Joyce Fantasy Football

Local tool for a 22-team, hand-run fantasy football league (Steve Joyce's
league, 36th season). Draft assistance, weekly lineups, trades, and
league-wide team tracking.

**This project is standalone.** Do not import from, reference, or reuse code
from any other project on this machine.

## League facts that drive everything
- 22 teams, two 11-team conferences (Blue/Red). 15-game season.
- 8 of 11 make playoffs per conference. Tiebreak: H2H > conf record >
  total conf pts > coin flip.
- **Drafted roster (11):** C(coach), K, DEF/ST, QB, RB x3, R x4.
- **Weekly starters (9):** C, K, DEF/ST, QB, RB x2, R x3. The "bench" is only
  the 1 RB + 1 R you don't start. No separate bench, no IR.
- **C/K/DEF/QB are NFL TEAM UNITS, not individual players.** You own
  "Seattle's QB room", "New England's kicker", "the Saints DEF/ST", a coach.
  All of a team's passing production aggregates to its QB slot. Backups
  scoring for you carry no individual injury risk. ~32 of each exist
  league-wide; 22 teams need one -> extreme scarcity.
- RB and R slots are individual players. R = WR + TE combined, no limits
  (0-4 TEs allowed).
- Coach scores 3 pts per NFL team win (a 14-win team's coach = 42 pts).
- **SCORING IS THRESHOLD-BASED, NOT PER-YARD.** 74 rush yds = 0 pts.
  75 = 2 pts. See `joyce_ff/scoring/rules.py` for the full ladder.
- **Therefore: NEVER average yardage and then score it.** Model the per-game
  distribution and integrate against the step function.
- Replacement level is calibrated for 22 teams (44 started RB, 66 started R;
  66 rostered RB, 88 rostered R). Public rankings/ADP are for 10-12 teams and
  DO NOT apply.
- Full rulebook: see `BRIEF.md.txt` and `joyce_ff/scoring/rules.py`.

## Stack
- Python 3.12 + SQLite, local web UI, no cloud services, no accounts.
- Scoring engine (Phase 1) has ZERO runtime dependencies (pure functions).
- Python lives at `.venv/` (created locally; not committed).

## Commands
- Run tests:  `python -m pytest`   (or `make test` / `./run.ps1 test`)
- Validate scoring vs site:  `python manage.py validate`  (Phase 1 harness)
- Sync data:  `python manage.py sync`   (one polite fetch per run)
- Run UI:  `python manage.py run`   (Phase 2+)

## Conventions
- League site https://joyce401.jimdofree.com/ is READ ONLY. Cache
  aggressively, one fetch per run. Never post to it or log in.
- Site HTML is hand-maintained and inconsistent. Parse defensively, fail
  loudly, never guess at an unparseable value.
- Every scrape appends a timestamped snapshot. Never overwrite history —
  the diffs are how we detect roster moves.
- **Never invent a stat or projection.** A failed source is a VISIBLE ERROR,
  not a plausible-looking made-up number. A blank cell is fine.
- Show the reasoning behind every recommendation (see `ScoreBreakdown`).
- Timestamp all data in the UI so staleness is visible.
- NFL stats come from nflverse (same official numbers as nfl.com, packaged
  for programmatic use); cached to SQLite.

## Open questions still with the commissioner (Scott's dad)
Answered so far: Q1 (team units ✓), Q2 (R=WR+TE, no limits ✓),
Q3 (no bench/IR ✓), Q4 (1pt/50yds beyond 450 passing ✓), Q5 (DEF/ST gets ST
pts; duplicate pts across owners ✓).
Still open: Q6 draft format/keeper, Q7 waivers/FA, Q8 trade rules,
Q9 schedule build, Q10 which team is ours + draft slot.
Engine assumptions awaiting confirmation live in `rules.ASSUMPTIONS` and are
what the Phase-1 reconciliation is designed to arbitrate.

## Decisions made
- 2026-08-07: C/K/DEF/QB modeled as NFL team units (confirmed).
- 2026-08-07: No existing commissioner spreadsheet; scrape + manual CSV
  fallback is the league-state source.
- 2026-08-07: Only current-season site data available for validation.
- 2026-08-07: NFL stats via nflverse, cached to SQLite.
- 2026-08-07: Scoring engine built dependency-free; 112 boundary/slot tests
  passing before any data layer.
