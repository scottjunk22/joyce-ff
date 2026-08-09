"""
Build a fully-populated DEMO league in one shot, scored on REAL 2025 results
through the validated engine — so standings, box scores, and the elimination
pool are genuine, not mocked. Rosters/lineups are auto-generated for the demo.

    python manage.py demo-seed      # rebuilds data/league.sqlite as the demo
"""

from __future__ import annotations

from pathlib import Path

from . import auth, repo, schema, scoring, setup
from . import standings as st

DEMO_WEEKS = 3
UNIT_OFFSET = {"QB": 0, "K": 8, "DEF/ST": 16, "C": 24}


def _auto_draft(conn, season_id):
    units = [r["abbr"] for r in conn.execute(
        "SELECT abbr FROM nfl_teams WHERE season_id=? ORDER BY abbr", (season_id,))]
    rbs = [r["gsis_id"] for r in conn.execute(
        "SELECT gsis_id FROM nfl_players WHERE season_id=? AND position='RB' ORDER BY name",
        (season_id,))]
    rs = [r["gsis_id"] for r in conn.execute(
        "SELECT gsis_id FROM nfl_players WHERE season_id=? AND position IN ('WR','TE') ORDER BY name",
        (season_id,))]
    for conf in ("BLUE", "RED"):
        teams = [r["id"] for r in conn.execute(
            "SELECT t.id FROM teams t JOIN conferences c ON c.id=t.conference_id "
            "WHERE t.season_id=? AND c.code=? ORDER BY t.draft_slot", (season_id, conf))]
        for i, tid in enumerate(teams):
            for slot in ("C", "K", "DEF/ST", "QB"):
                abbr = units[(i + UNIT_OFFSET[slot]) % len(units)]
                conn.execute(
                    "INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,"
                    "unit_type,roster_slot,acquired_ff_week,acquired_via,created_at) "
                    "VALUES (?,?,'TEAM_UNIT',?,?,?,1,'DRAFT','demo')", (season_id, tid, abbr, slot, slot))
            for j in range(3):
                conn.execute(
                    "INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,"
                    "roster_slot,acquired_ff_week,acquired_via,created_at) "
                    "VALUES (?,?,'PLAYER',?, 'RB',1,'DRAFT','demo')", (season_id, tid, rbs[i * 3 + j]))
            for j in range(4):
                conn.execute(
                    "INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,"
                    "roster_slot,acquired_ff_week,acquired_via,created_at) "
                    "VALUES (?,?,'PLAYER',?, 'R',1,'DRAFT','demo')", (season_id, tid, rs[i * 4 + j]))
    conn.commit()


def _set_default_lineup(conn, season_id, team_id, ff_week):
    """Start units + first 2 RB + first 3 R from the current roster (2-3 legal)."""
    roster = repo.current_roster(conn, team_id)
    by_slot = {s: [e for e in roster if e["roster_slot"] == s] for s in ("C", "K", "DEF/ST", "QB", "RB", "R")}
    starters = ([{"roster_slot": s, "asset_ref": by_slot[s][0]["asset_ref"]} for s in ("C", "K", "DEF/ST", "QB")]
                + [{"roster_slot": "RB", "asset_ref": e["asset_ref"]} for e in by_slot["RB"][:2]]
                + [{"roster_slot": "R", "asset_ref": e["asset_ref"]} for e in by_slot["R"][:3]])
    repo.set_lineup(conn, season_id, team_id, ff_week, starters)


def _demo_transactions(conn, season_id):
    """A few trades so Transactions + fees have content, plus one payment."""
    for name in ("OT Blitz", "Pike", "Cooper"):
        tid = conn.execute("SELECT id FROM teams WHERE name=?", (name,)).fetchone()["id"]
        conf = conn.execute("SELECT conference_id FROM teams WHERE id=?", (tid,)).fetchone()["conference_id"]
        out = [e for e in repo.current_roster(conn, tid) if e["roster_slot"] == "RB"][-1]["asset_ref"]
        avail = repo.available_players(conn, season_id, conf, "RB")
        if avail:
            repo.do_trade(conn, season_id, tid, "RB", out, avail[0]["gsis_id"], ff_week=2)
    otb = conn.execute("SELECT id FROM teams WHERE name='OT Blitz'").fetchone()["id"]
    repo.record_payment(conn, season_id, otb, 200, note="paid Wk2 trade", actor="commissioner")


def build_demo(db_path: str | Path = schema.DEFAULT_DB_PATH) -> dict:
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    conn = schema.connect(db_path)
    schema.init_db(conn)
    sid = schema.seed_reference(conn, year=2025, label="2025-26")
    setup.prepare_season(conn, sid, 2025)
    _auto_draft(conn, sid)

    for wk in range(1, DEMO_WEEKS + 1):
        scoring.ingest_asset_scores_from_nflverse(conn, sid, wk)
        for tid in [r["id"] for r in conn.execute("SELECT id FROM teams WHERE season_id=?", (sid,))]:
            _set_default_lineup(conn, sid, tid, wk)
        scoring.score_team_week(conn, sid, wk)
        st.run_elimination(conn, sid, wk)

    conn.execute("UPDATE seasons SET current_ff_week=?, status='active' WHERE id=?", (DEMO_WEEKS, sid))
    # Demo is a completed season — don't enforce kickoff locks so lineups stay editable.
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('enforce_locks','0')")
    _demo_transactions(conn, sid)

    for tid in [r["id"] for r in conn.execute("SELECT id FROM teams WHERE season_id=?", (sid,))]:
        auth.set_team_passcode(conn, tid, "demo")
    for admin in ("Steve", "Scott"):
        auth.set_admin_passcode(conn, admin, "commish")
    conn.commit()

    pool = st.pool_status(conn, sid)
    conn.close()
    return {"season_id": sid, "weeks": DEMO_WEEKS,
            "alive": len(pool["alive"]), "eliminated": len(pool["eliminated"])}
