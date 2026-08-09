# Joyce Fantasy Football

Local tool for a 22-team, hand-run fantasy football league (Steve Joyce's
league, 36th season). Draft assistance, weekly lineups, trades, and
league-wide team tracking.

**This project is standalone.** Do not import from, reference, or reuse code
from any other project on this machine.

## League facts that drive everything
- 22 teams, two 11-team conferences/divisions (Blue/Red). 15-game season.
- **DRAFT IS PER DIVISION (11 teams), SEPARATE POOLS.** Each division holds its
  own draft from the FULL NFL pool; the same NFL player/team-unit can be owned
  in BOTH divisions at once. So all draft math (replacement, VOR, scarcity) is
  11-team, NOT 22. Our team OT Blitz drafts in the Blue division vs 10 others.
  The 22-team number only governs season structure (standings/schedule/SB).
- 8 of 11 make playoffs per conference. Tiebreak: H2H > conf record >
  total conf pts > coin flip.
- **Drafted roster (11):** C(coach), K, DEF/ST, QB, RB x3, R x4.
- **Weekly starters (9):** C, K, DEF/ST, QB, RB x2, R x3. The "bench" is only
  the 1 RB + 1 R you don't start. No separate bench, no IR.
- **C/K/DEF/QB are NFL TEAM UNITS, not individual players.** You own
  "Seattle's QB room", "New England's kicker", "the Saints DEF/ST", a coach.
  All of a team's passing production aggregates to its QB slot. Backups
  scoring for you carry no individual injury risk. ~32 of each exist; only
  ~11 owned per division -> team units are ABUNDANT (two-thirds available),
  NOT scarce. (The original brief's "extreme scarcity" assumed a shared
  22-team pool; the per-division separate-pool draft overturns that.)
- RB and R slots are individual players. R = WR + TE combined, no limits
  (0-4 TEs allowed).
- Coach scores 3 pts per NFL team win (a 14-win team's coach = 42 pts).
- **SCORING IS THRESHOLD-BASED, NOT PER-YARD.** 74 rush yds = 0 pts.
  75 = 2 pts. See `joyce_ff/scoring/rules.py` for the full ladder.
- **Therefore: NEVER average yardage and then score it.** Model the per-game
  distribution and integrate against the step function.
- Replacement level is calibrated for ONE DIVISION = 11 teams (22 started RB,
  33 started R; 33 rostered RB, 44 rostered R). Public rankings/ADP are for
  10-12 teams and DO NOT apply.
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

## Open questions status (commissioner = Scott's dad)
Answered: Q1 (team units ✓), Q2 (R=WR+TE, no limits ✓), Q3 (no bench/IR ✓),
Q4 (1pt/50yds beyond 450 passing ✓), Q5 (DEF/ST gets ST pts; duplicate pts
across owners ✓), Q6 (draft order ✓, card-draw table in joyce_ff/draft),
Q7 (waivers ✓), Q8 (no trades ✓), Q10 (team = OT Blitz ✓).
Still open: our draft SLOT for 2026-27 (the Spades card, drawn on draft day).
Q9 schedule STRUCTURE now known (see below); only the 2026-27 FF-week/NFL-week
offset needs confirming.

### Q6 — Draft order (confirmed 2026-08-08)
- Card-draw: each team draws a Spades card 1-11 = their pick SLOT. The table
  maps slot -> pick number per round (11 rounds, 4-round repeating cycle).
  Reduced from dad's 12-team PDF by dropping the 12th slot (Scott fixed a
  transcription error in R4/R8). Encoded + validated in joyce_ff/draft/order.py.
- Live pick grid built (web UI "Draft Room"): on-the-clock, your next pick +
  picks-until, roster fill, best-available-for-need, and "won't survive to
  your next pick" at-risk line. Our slot is TBD until draft day.

### Q9 — Schedule structure (from the 2025-26 schedule, confirmed 2026-08-08)
- 15 FF games. FF Weeks 1-4 = INTERLEAGUE (Red vs Blue, opposite conference);
  FF Weeks 5-15 = CONFERENCE 11-team round-robin (5 games + 1 bye each week);
  FF Week 16 = No Play. Matchups set by Team# draws, not the NFL schedule.
- 2025-26 ran FF Weeks 1-15 over NFL Weeks 3-17 (NFL Weeks 1-2 = FF pre-season,
  NFL Week 18 = No Play). Encoded in `joyce_ff/schedule/`. The FF-Week-1 =
  NFL-Week-3 offset is assumed for 2026-27 (ASSUMPTION: confirm per season).
- NOTE: the 2025-26 Excel had a typo (FF Wk1 R10 vs B13 -> should be B3);
  Scott corrected it in the file. The scanned PDF is the source of truth.
Engine assumptions awaiting confirmation live in `rules.ASSUMPTIONS` and are
what the Phase-1 reconciliation is designed to arbitrate.

### Q7 — Waivers / free agency / lineup locks (confirmed 2026-08-08)
- First-come, first-serve. Any owner may swap one of their own players for an
  available one at any time, as long as the pickup's NFL game hasn't started.
- No waiver deadline/priority/FAAB.
- Lineup lock is PER PLAYER at his game's kickoff: starters due Sunday noon,
  but a Thursday-night player must be started before his Thursday game begins.

### Q8 — Trades (confirmed 2026-08-08)
- No trade rules; trading effectively doesn't happen in this league.
- => Phase 4 (trade analyzer/finder) is DE-SCOPED unless requested later.
  Still scrape the Trade Offers page for completeness.

### Draft-day mechanics (confirmed 2026-08-08)
- Two separate draws per team on draft day:
  1. A **Team #** -> determines that team's SCHEDULE (whom they play).
     OT Blitz was Team #4 in 2025-26.
  2. A **draft-order #** -> determines pick position for drafting players.
- 2026-27 draft has not happened yet; both numbers are TBD for us.

## Decisions made
- 2026-08-07: C/K/DEF/QB modeled as NFL team units (confirmed).
- 2026-08-07: No existing commissioner spreadsheet; scrape + manual CSV
  fallback is the league-state source.
- 2026-08-07: Only current-season site data available for validation.
- 2026-08-07: NFL stats via nflverse, cached to SQLite.
- 2026-08-07: Scoring engine built dependency-free; 112 boundary/slot tests
  passing before any data layer.
- 2026-08-08: nfl_data_py abandoned (pins pandas w/o py3.12 wheel, fails to
  build). We read nflverse release files directly: play-by-play parquet +
  schedules csv, cached to data/nflverse_cache/. Same underlying numbers.
- 2026-08-08: 2025 per-player weekly stats not yet published by nflverse, but
  2025 play-by-play IS complete — so we DERIVE all per-game stats (incl. FG
  distances, DEF/ST box scores, return TDs) from PBP. Better single source.
- 2026-08-08: PHASE 1 VALIDATED. `manage.py validate` reconciles the engine
  vs the site's posted per-slot scores. The site currently exposes 2 filled
  lineups (Super Bowl week = NFL 2025 wk22, SEA vs NE). All 18 slot values +
  both totals (26, 44) match exactly. Week auto-identified from team-unit
  anchors; player names auto-resolved; nothing fabricated.
- 2026-08-08: Site team-name attribution in lineup tables is UNRELIABLE
  (hand-maintained HTML has inconsistent per-row cell counts; a filled column
  can land one team off — e.g. shows ~'Pike' for what is likely BGH). Slot
  VALUES parse reliably; the fantasy-team LABEL does not. Fix in Phase 2.
- 2026-08-08: Q10 partially answered — our team is OT Blitz (Blue conf).
- 2026-08-08: DRAFT IS PER-DIVISION, SEPARATE POOLS (confirmed). Recalibrated
  all draft valuation from 22-team to 11-team: replacement/VOR/scarcity now
  division-based. Team units reclassified from "extreme scarcity" to
  "abundant" (~11 of 32 owned per division). Individual RB/R are the scarce
  assets. rules.py gained DIVISION_* constants; valuation/market/board repointed.
