#!/usr/bin/env python
"""
Single entry point for the Joyce FF tool.

Usage:
    python manage.py test        # run the scoring engine test suite
    python manage.py initdb      # create the SQLite schema
    python manage.py validate    # Phase 1: reconcile engine vs posted scores
    python manage.py board       # Phase 2: value-over-replacement draft boards
    python manage.py sync        # one polite fetch of league state (Phase 2+)
    python manage.py run         # launch the local web UI (Phase 2+)

Kept dependency-light so `test`/`initdb` work with only the standard library.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def cmd_test(_argv: list[str]) -> int:
    return subprocess.call([sys.executable, "-m", "pytest", *_argv])


def cmd_initdb(_argv: list[str]) -> int:
    from joyce_ff.db import connect, init_db

    conn = connect()
    init_db(conn)
    conn.close()
    print("Initialized SQLite schema at data/joyce_ff.sqlite")
    return 0


def cmd_validate(argv: list[str]) -> int:
    from scripts.validate_scoring import main as validate_main

    return validate_main(argv)


def cmd_board(argv: list[str]) -> int:
    from scripts.draft_board import main as board_main

    return board_main(argv)


def cmd_board_cache(_argv: list[str]) -> int:
    """(Re)build data/boards.json from cached nflverse history — the payload the
    private OT-Blitz Valuation Board serves. Run on the host after a data pull."""
    from joyce_ff.projections.board import build_and_cache

    print("Building draft-board cache (scoring 2023-2025)...")
    data = build_and_cache()
    print(f"Wrote data/boards.json — {len(data['players'])} players, "
          f"generated {data['generated_at']}.")
    return 0


def cmd_set_platform_pass(argv: list[str]) -> int:
    """Set the private OT-Blitz platform passcode (Scott's eyes only)."""
    from joyce_ff.league import connect
    from joyce_ff.league import auth

    if not argv:
        print("usage: set-platform-pass <passcode>", file=sys.stderr)
        return 1
    conn = connect()
    auth.set_platform_passcode(conn, argv[0])
    conn.close()
    print("OT-Blitz platform passcode set.")
    return 0


def cmd_market(argv: list[str]) -> int:
    from scripts.market_report import main as market_main

    return market_main(argv)


def cmd_schedule(argv: list[str]) -> int:
    from scripts.schedule_report import main as sched_main

    return sched_main(argv)


def cmd_league_init(_argv: list[str]) -> int:
    from joyce_ff.league import connect, init_db, seed_reference

    conn = connect()
    init_db(conn)
    season_id = seed_reference(conn)
    teams = conn.execute("SELECT COUNT(*) c FROM teams WHERE season_id=?",
                         (season_id,)).fetchone()["c"]
    conn.close()
    print(f"League DB ready at data/league.sqlite — season {season_id}, "
          f"{teams} teams seeded (Blue + Red), 2 commissioners.")
    return 0


def cmd_new_season(argv: list[str]) -> int:
    from joyce_ff.league import connect, setup

    year = int(argv[0]) if argv else 2026
    conn = connect()
    try:
        sid = setup.create_season(conn, year)
    except (ValueError, RuntimeError) as e:
        print(f"Cannot create season: {e}")
        conn.close()
        return 1
    row = conn.execute("SELECT label FROM seasons WHERE id=?", (sid,)).fetchone()
    teams = conn.execute("SELECT COUNT(*) c FROM teams WHERE season_id=?", (sid,)).fetchone()["c"]
    conn.close()
    print(f"Created season {sid} ({row['label']}) — {teams} teams, schedule generated. "
          f"The site now shows this season at Week 1; set team names/numbers in the "
          f"commissioner tab, then draft in the OT-Blitz Draft Room.")
    return 0


def cmd_demo_seed(_argv: list[str]) -> int:
    from joyce_ff.league.demo import build_demo

    print("Building demo league (loading NFL data + scoring 3 real weeks)...")
    r = build_demo()
    print(f"Demo ready at data/league.sqlite — season {r['season_id']}, "
          f"{r['weeks']} weeks scored, {r['alive']} alive / {r['eliminated']} eliminated.")
    print("Team passcode: 'demo' · Commissioner passcode: 'commish'. Run: python manage.py serve")
    return 0


def cmd_serve(argv: list[str]) -> int:
    from joyce_ff.webapp import create_app

    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 5000
    app = create_app()
    print(f"Serving the league site at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=port, debug="--debug" in argv)
    return 0


def cmd_run_week(argv: list[str]) -> int:
    from joyce_ff.league import connect
    from joyce_ff.league.runner import reconcile_week, run_week

    if not argv or not argv[0].isdigit():
        print("usage: run-week N", file=sys.stderr)
        return 1
    wk = int(argv[0])
    conn = connect()
    sid = conn.execute("SELECT id FROM seasons ORDER BY year DESC LIMIT 1").fetchone()["id"]
    s = run_week(conn, sid, wk)
    elim = s.get("eliminated_team_id")
    ename = conn.execute("SELECT name FROM teams WHERE id=?", (elim,)).fetchone() if elim else None
    print(f"Week {wk}: scored {len(s['team_scores'])} teams"
          + (f", scored {s['assets_scored']} NFL assets" if 'assets_scored' in s else "")
          + (f"; eliminated {ename['name']}" if ename else ""))
    rec = reconcile_week(conn, sid, wk)
    if rec["checked"]:
        print(f"Reconcile vs legacy site: {rec['matched']}/{rec['checked']} match"
              + (f"  MISMATCHES: {rec['mismatches']}" if rec["mismatches"] else ""))
    conn.close()
    return 0


def cmd_run_current(_argv: list[str]) -> int:
    from joyce_ff.league import connect
    from joyce_ff.league.runner import run_current

    conn = connect()
    sid = conn.execute("SELECT id FROM seasons ORDER BY year DESC LIMIT 1").fetchone()["id"]
    ran = run_current(conn, sid)
    print(f"Auto-scored FF weeks: {ran or 'none ready yet'}")
    conn.close()
    return 0


def cmd_sync(_argv: list[str]) -> int:
    from joyce_ff.league import connect
    from joyce_ff.league.scrape import scrape_and_store

    conn = connect()
    s = conn.execute("SELECT id, current_ff_week FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
    r = scrape_and_store(conn, s["id"], s["current_ff_week"])
    print(f"Scraped legacy site (snapshot {r['sha']}): stored {r['matched']} posted totals"
          + (f"; unmatched labels: {r['unmatched']}" if r["unmatched"] else ""))
    conn.close()
    return 0


def cmd_run(argv: list[str]) -> int:
    from joyce_ff.web.server import run as web_run

    return web_run(argv)


COMMANDS = {
    "test": cmd_test,
    "initdb": cmd_initdb,
    "validate": cmd_validate,
    "board": cmd_board,
    "board-cache": cmd_board_cache,
    "set-platform-pass": cmd_set_platform_pass,
    "market": cmd_market,
    "schedule": cmd_schedule,
    "league-init": cmd_league_init,
    "new-season": cmd_new_season,
    "demo-seed": cmd_demo_seed,
    "run-week": cmd_run_week,
    "run-current": cmd_run_current,
    "serve": cmd_serve,
    "sync": cmd_sync,
    "run": cmd_run,
}


def main() -> int:
    # Windows consoles default to cp1252; force UTF-8 so unicode output (Δ, ×,
    # …) never crashes a command mid-report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 1
    sys.path.insert(0, str(ROOT))
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
