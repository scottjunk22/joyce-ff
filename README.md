# Joyce Fantasy Football

Local, offline-first tools for **Steve Joyce's 22-team, hand-run fantasy
football league** (36th season). This league is not on any commercial
platform, has no API, and uses a **custom threshold-based scoring system**.
Standard fantasy tools do not apply here.

Runs entirely on your machine: Python + SQLite, local web UI. No cloud, no
accounts, no hosting.

## Quick start (Windows)

```powershell
.\run.ps1            # bootstraps .venv and runs the test suite
.\run.ps1 validate   # Phase 1: reconcile the engine vs the site's posted scores
```

Or with `make` (Git Bash / WSL):

```bash
make venv && make test
```

## What makes this league different
- **22 teams**, two 11-team conferences (Blue/Red), 15-game season.
- **Threshold scoring**: yardage pays in step functions. 74 rushing yards = 0
  points; 75 = 2. You cannot average yards and convert to points.
- **Team-unit slots**: Coach, Kicker, DEF/ST, and QB are drafted as NFL *team
  units*, not individual players. RB and R (WR/TE) are individuals.
- **Draft 11, start 9** each week (RB: start 2 of 3; R: start 3 of 4).

Full rules: [`BRIEF.md.txt`](BRIEF.md.txt) and
[`joyce_ff/scoring/rules.py`](joyce_ff/scoring/rules.py).

## Status
- **Phase 1 — scoring engine**: implemented, 112 boundary/slot tests passing.
  Reconciliation against the site's posted weekly scores is in progress.
- Phases 2-5 (draft assistant, lineup optimizer, trade tools, league
  intelligence): not started.

## Layout
```
joyce_ff/scoring/   # the rulebook (rules.py) + pure scoring engine (engine.py)
joyce_ff/db/        # SQLite schema; append-only timestamped snapshots
scripts/            # validation / reconciliation harness
tests/              # exhaustive threshold-boundary tests
manage.py           # single entry point: test | initdb | validate | sync | run
```

## Ground rules baked into the code
- The league site is **read-only**. One polite fetch per run; never post/log in.
- Every scrape is an **append-only** timestamped snapshot; history is never
  overwritten (diffs detect roster moves).
- **Never fabricate a stat or score.** A failed source is a visible error.
- Every recommendation shows its reasoning.
