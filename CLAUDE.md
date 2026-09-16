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
- **Replacement is the first asset still AVAILABLE, i.e. N+1, not N.** 33 RBs
  get drafted, so the 33rd is the LAST ONE TAKEN and the replacement is the
  **34th** — likewise the **45th** R and the **12th** of each team unit (11 of
  ~32 owned). VOR = proj - that. Getting this wrong is an easy off-by-one (it
  shipped that way once) and it is not cosmetic: each slot's baseline moves by
  a different amount, so it changes cross-position ordering. The same N+1 index
  is used for the shrinkage prior, so both mean the same thing.
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
- WEEKLY SCORING reads ESPN's public game feed first (joyce_ff/data_sources/
  espn.py): complete within minutes of the final whistle, where nflverse's
  scheduled updates run hours late, get skipped, or fail. nflverse is the
  backup and the later cross-check. A game locks from whichever source first
  has it complete; ESPN locks only after 10 minutes at Final and only if every
  scoring play is classified and every player with stats is matched. ESPN is
  unofficial — it rejects a descriptive User-Agent (use plain "Mozilla/5.0").
  The draft board / projections stay on nflverse history.

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
- 2026-09-16: DEF/ST SACKS = times the opponent's QB was sacked (ESPN team stat
  "sacksYardsLost", e.g. "4-16"), not the sum of individual defenders — a sack
  credited to nobody still counts. KC 2026 Week 1 had 4 (one uncredited); ESPN
  had scored 3. nflverse already counted sack plays. All 32 Week 1 defenses now
  agree between the two readers.
- 2026-09-16: QB SLOT RUNNING/CATCHING (commissioner). The QB slot scores its
  QBs' (roster position QB) COMBINED rushing yards, receiving yards, receptions
  on the normal ladders, 6 per rushing/receiving TD, 2 per conversion scored —
  plus ALL team passing yards (any passer) but TD passes ONLY when a QB threw
  them. An RB/receiver who throws a TD pass gets the 3; his passing yards go to
  the QB slot, not him. RBs/receivers already scored rushing + receiving +
  receptions together. Week 1 had 9 uncounted QB rushing TDs (BUF 2, CHI 2,
  BAL, CAR, KC, MIA, TB); ESPN and nflverse agree on all 32 QB slots under the
  new rule. Locked lines surface as stat checks for Change — the nflverse
  cross-check now re-compares EVERY locked game, not just ESPN-locked ones, so
  a rule change reaches lines that locked from nflverse (most of practice
  Week 1 locked with source NULL, before that column existed). A stat check
  keeps the other source's ITEMIZED lines (stat_checks.other_breakdown), so
  Change replaces the whole breakdown and the box score explains the new total,
  with a 0-point note line "corrected from nflverse · was N" (0-point items
  render as notes). When totals already agree the itemization is refreshed in
  place, notes kept. Breakdowns name TDs by how they were scored (rushing /
  receiving / return / QB rushing).
- 2026-09-15: COMMISSIONER TAB LAYOUT (commissioner). Order: Needs your
  attention (ties, stat checks with Change/Keep buttons, check-lineup notes;
  else a green "nothing") -> This week (lineups count, PIN count only while a
  team lacks one, score double-check list) -> Tools (Weekly points) -> Teams ->
  folded rows (Score now, Earlier score checks, moves, acting for a manager, PIN
  setup, season setup). Score double-check (league/score_checks.py,
  commissioner-only endpoint): a game is checked once nflverse was compared
  (nfl_game_locks.verified_at) or it locked from nflverse. Up to 3 weeks on top:
  every unfinished week, then finished newest first; the rest under Earlier. A
  game nflverse never posts in the 4-day window keeps its week listed until
  dismissed. A "Change" on a stat check rewrites that line (breakdown note) and
  re-totals the week; eliminations already made are not redone.
- 2026-09-15: PIN RESET (commissioner). "Reset PIN" clears one team's PIN and
  opens only that team to set a new one from its roster, closing when the
  manager does (auth.reset_team_pin / pin_resets). The commissioner never picks
  a manager's PIN; the old type-a-PIN box is gone. MuddyChicks is the
  renamed Muddy Chickens (champions list stays verbatim).
  2026-09-16: the league-wide "Let managers set their PIN" switch is GONE (it
  opened Red teams at the Blue draft). PIN setup opens by conference ("Open
  PINs for Blue/Red", at each draft night) or per team ("Open PIN" on a PIN-less
  team); an opened team STAYS open until its manager sets a PIN (many do it at
  home), then closes itself. "Close PIN setup" / per-team close exist but are
  optional. Code: auth.open_conference_pins / open_team_pin / pin_opens.
- 2026-09-15: OPENS IN LINEUPS (commissioner). Buying an Open swaps the rental
  into an already-saved lineup that starts the bye player it covers (no
  resubmit); reversing the Open swaps him back (repo.do_open /
  reverse_transaction). Set Lineup pre-selects an Open over the player it
  covers, shows it with the blue OPEN tag and its game time. Its bottom line is
  green only for a lineup submitting accepts (repo.bye_flex mirrors set_lineup).
  2026-09-16: a manager can't Open a player whose game has kicked off
  (repo.do_open locked_refs; commissioner exempt). Roster and Set Lineup list a
  rental DIRECTLY UNDER the bye player it covers ("↳ Jonnu Smith MIA OPEN").
  Box score rental row: "Jonnu Smith OPEN · Kelce", always one line (ellipsis,
  never wraps, so rows stay level across teams; covered name last so a trim
  drops it before the game state) and always tappable, even at 0, opening
  with "Open for Travis Kelce (bye)".
- 2026-09-15: LINEUP LABELS (commissioner). Most managers submit Sunday
  morning, so cards only say: "⚠ Thu player" (team hasn't submitted and has an
  RB/R whose game is before Sunday; tap opens Set Lineup) until that kickoff;
  grey "last week's lineup" Sunday 8am-noon CT for a copied lineup; nothing
  from Sunday noon (label takes the "to play" spot until then). Who never
  submitted and "check lineup" live in the commissioner tab. The copy IS the
  official lineup; the commissioner doesn't re-submit. Set Lineup shows each
  player's game day/time. Code: progress.lineup_flags.
  Also: a banner above the scoreboard, Tuesday 6am until each pre-Sunday
  kickoff, one line per game ("SF @ LAR · Thu 7:15 PM — no lineup yet, with a
  player in this game: A · B"), team names only, tap opens Set Lineup
  (progress.early_lineup_alerts). "N to play" is hidden for everyone until
  Sunday noon CT (progress.counts_visible). A copied lineup is never called
  submitted (weekly_lineups.carried_from). Box scores are public, so a copied
  lineup's note ("last week's lineup", grey, BELOW the rows so slots line up
  across teams) shows only from Sunday 8am CT on. Set Lineup banner: no lineup +
  pre-Sunday player -> amber, names him and his kickoff; no lineup otherwise ->
  grey "No lineup set for Week N yet." only (never say last week's lineup "is
  used" to a Sunday-only team); copied -> grey "in place, change any player whose
  game hasn't started" until Sunday 8am CT, then amber (progress.lineup_notice).
- 2026-09-15: TWO WEEK CLOCKS (commissioner). LINEUP week (lineups, trades,
  Opens, lineups-in count; upcoming week added to the scoreboard dropdown)
  moves at 6am CT the Tuesday after a week's last game. SCOREBOARD week (the
  site's default view) moves at 6am CT on the day of the next week's first
  kickoff (Thursday). Dropdown newest-first; Week left of Season. Code:
  progress.lineup_week / scoreboard_week. Never use seasons.current_ff_week
  directly for either.
- 2026-09-14: TIED GAMES (commissioner). A matchup tied on points goes to the
  team whose STARTED DEF/ST (a rented one counts) allowed the fewest NET yards.
  A DEF/ST on bye not covered by an Open loses. Both on bye, or equal yards:
  commissioner's discretion (no further tiebreaker; "Ties to decide" in the
  commissioner tab). Applies to every matchup incl. playoffs/Super Bowl. NOT to
  the elimination pool (tie for lowest still eliminates all tied). Standings
  show W-L only. Code: joyce_ff/league/tiebreak.py; DEF yards kept in
  asset_week_scores.yards_allowed (`manage.py fill-def-yards` backfills).
- 2026-09-14: NET YARDS (commissioner). QB-slot passing yards are NET (gross
  minus sack yards); both readers had used gross. DEF yards allowed was already
  the opponent's net total yards. Week 1 impact: CIN QB 254 gross -> 245 net
  (3 -> 0 pts), NO 410 -> 378 (6 -> 5). Board QB history follows after
  `manage.py board-cache`.
- 2026-09-14: TWO-POINT CONVERSIONS (commissioner). The player who scores a
  successful 2-pt conversion (catch or run) gets 2; the team whose QB THROWS
  one gets 1 on its QB slot — this is the rulebook's "Extra point pass = 1"
  (ASSUMPTIONS A5, now on). Both the ESPN and nflverse readers had been feeding
  zero conversions, so Week 1's one (Jefferson, GB@MIN) scored 18, not 20.
- 2026-08-11: COMMISSIONER ANSWERS (dad). (1) Per-player kickoff lock is
  sufficient — NO separate Sunday-noon deadline. (2) Unset lineup carries
  forward previous week; commissioner can adjust if needed. (3) ELIMINATION:
  a tie for the LOWEST score eliminates ALL tied teams (no tiebreak), every
  week AND Week 15 (implemented in standings.run_elimination). Week-15 payout:
  top score $100 (ties for top SPLIT it); every other remaining survivor $10.
  (4) 2026-27 schedule offset (FF wk1 = NFL wk3) still believed correct.
  (5) Draft-order table confirmed good.
- 2026-08-11: FEE MODEL (dad). Entry fee $80/team, a line item under each
  roster's fees. It INCLUDES 5 free trades; the 6th+ trade costs the usual
  fee. Each roster must show free-trades-used (n/5). OPEN vs trade counting
  against the 5 free = TBD (ask). [Not yet implemented.]
- 2026-08-11: PENDING FEATURES (dad requests, to design): (A) $80 entry-fee
  line + 5-free-trades counter per roster; (B) mark Open players + list Bye
  teams above/below scoreboard and flag Bye/Open players within rosters;
  (C) click a player in a box score to see his point breakdown (from scoring
  ScoreBreakdown).
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
