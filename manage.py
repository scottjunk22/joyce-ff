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


def cmd_sync(_argv: list[str]) -> int:
    print("sync is not implemented yet (Phase 2). No data was fetched.",
          file=sys.stderr)
    return 2


def cmd_run(argv: list[str]) -> int:
    from joyce_ff.web.server import run as web_run

    return web_run(argv)


COMMANDS = {
    "test": cmd_test,
    "initdb": cmd_initdb,
    "validate": cmd_validate,
    "board": cmd_board,
    "sync": cmd_sync,
    "run": cmd_run,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 1
    sys.path.insert(0, str(ROOT))
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
