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


def cmd_load_champions(_argv: list[str]) -> int:
    from joyce_ff.league import connect, schema
    from joyce_ff.league.champions_seed import rows

    conn = connect()
    schema.migrate(conn)      # the CLI doesn't migrate on connect; runner_up may be new
    n = 0
    for year, label, team, runner in rows():
        conn.execute(
            "INSERT INTO champions(year,label,team,runner_up) VALUES (?,?,?,?) "
            "ON CONFLICT(year) DO UPDATE SET label=excluded.label, team=excluded.team, "
            "runner_up=excluded.runner_up", (year, label, team, runner))
        n += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) c FROM champions").fetchone()["c"]
    latest = conn.execute("SELECT label, team FROM champions ORDER BY year DESC LIMIT 1").fetchone()
    conn.close()
    print(f"Loaded {n} champions ({total} on file). Most recent: {latest['label']} {latest['team']}.")
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


def _league_databases():
    """The league, plus the practice universe when it exists — a rehearsal is
    only a rehearsal if it runs on the same clock."""
    from pathlib import Path

    from joyce_ff.league import schema

    main = Path(schema.DEFAULT_DB_PATH)
    dbs = [("league", main)]
    practice = main.with_name("league_dark.sqlite")      # same rule as the web app
    if practice.exists():
        dbs.append(("practice", practice))
    return dbs


def _score_databases(command: str, sources=None, only_while_games_on=False) -> int:
    from datetime import datetime, timezone

    from joyce_ff.league import connect, progress, schema
    from joyce_ff.league.runner import run_current

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    failed = 0
    for label, path in _league_databases():
        conn = connect(path)
        try:
            schema.migrate(conn)              # the CLI doesn't migrate on connect
            row = conn.execute("SELECT id FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
            if row is None:
                if not only_while_games_on:
                    print(f"[{stamp}] {command} ({label}): no season yet")
                continue
            if only_while_games_on and not progress.games_to_watch(conn, row["id"]):
                continue
            r = run_current(conn, row["id"], sources=sources)
            parts = []
            if r["scored"]:
                parts.append(f"finalized {r['scored']}")
            if r["live"]:
                parts.append(f"live-updated {r['live']}")
            if r.get("note"):
                parts.append(r["note"])
            print(f"[{stamp}] {command} ({label}): {'; '.join(parts) or 'nothing to score yet'}",
                  flush=True)
        except Exception as e:              # one database failing mustn't stop the other
            failed += 1
            print(f"[{stamp}] {command} ({label}) FAILED: {type(e).__name__}: {e}", flush=True)
        finally:
            conn.close()
    return failed


def cmd_run_current(_argv: list[str]) -> int:
    """The hourly job: ESPN and nflverse, for the league and the practice site."""
    return 1 if _score_databases("run-current") else 0


def cmd_fill_def_yards(_argv: list[str]) -> int:
    """One-time: record DEF/ST net yards allowed (the tied-game tiebreaker) for
    games scored before yards were kept. Only fills blanks — no score changes."""
    from joyce_ff.data_sources import nflverse as nv
    from joyce_ff.league import connect, schema, scoring

    for label, path in _league_databases():
        conn = connect(path)
        try:
            schema.migrate(conn)
            row = conn.execute("SELECT id, year FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
            if row is None:
                continue
            sid, year = row["id"], row["year"]
            weeks = [r["ff_week"] for r in conn.execute(
                "SELECT DISTINCT ff_week FROM asset_week_scores WHERE season_id=? "
                "AND unit_type='DEF/ST' AND yards_allowed IS NULL ORDER BY ff_week", (sid,))]
            if not weeks:
                print(f"{label}: nothing to fill")
                continue
            pbp = nv.load_pbp(year)
            pbp = pbp[pbp["season_type"].isin(["REG", "POST"])]
            dd = nv.defense_unit_week_stats(pbp, nv.load_games(), year)
            n = 0
            for ff in weeks:
                wk = scoring.nfl_week_for(conn, sid, ff)
                for r in dd[dd["week"] == wk].itertuples():
                    n += scoring.fill_yards_allowed(conn, sid, ff, r.team, r.yards_allowed)
            conn.commit()
            left = conn.execute("SELECT COUNT(*) c FROM asset_week_scores WHERE season_id=? AND "
                                "unit_type='DEF/ST' AND yards_allowed IS NULL", (sid,)).fetchone()["c"]
            print(f"{label}: filled yards allowed for {n} DEF/ST lines (weeks {weeks}); "
                  f"{left} still blank")
        finally:
            conn.close()
    return 0


def cmd_settle_report(_argv: list[str]) -> int:
    """Does ESPN change a box score after Final? Compares each game's copies
    taken at first Final, at lock, and 30 minutes after (league/settle.py)."""
    from joyce_ff.league import connect, schema, settle

    for label, path in _league_databases():
        conn = connect(path)
        try:
            schema.migrate(conn)
            row = conn.execute("SELECT id FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
            lines = settle.report(conn, row["id"]) if row else []
            print(f"== {label}: {len(lines) and 'games recorded' or 'nothing recorded yet'}")
            for line in lines:
                print(line)
        finally:
            conn.close()
    return 0


def cmd_live(argv: list[str]) -> int:
    """The always-on checker: every few minutes, while any game is kicked off
    and not yet locked, read ESPN and score — so a game is final on the site
    within minutes of the whistle. Sleeps the rest of the week. nflverse stays
    with the hourly job, which keeps this light."""
    import time

    every = int(argv[argv.index("--every") + 1]) if "--every" in argv else 300
    print(f"live: checking ESPN every {every}s while games are on", flush=True)
    while True:
        _score_databases("live", sources=("espn",), only_while_games_on=True)
        time.sleep(every)


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
    "load-champions": cmd_load_champions,
    "demo-seed": cmd_demo_seed,
    "run-week": cmd_run_week,
    "run-current": cmd_run_current,
    "live": cmd_live,
    "settle-report": cmd_settle_report,
    "fill-def-yards": cmd_fill_def_yards,
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
