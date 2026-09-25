"""The Rams show as LAR; the stored code stays LA.

nflverse's schedule calls the Rams "LA", which beside the Chargers' "LAC" reads
as either Los Angeles team. Every join depends on the stored code, so only what
people see changes — anything the site sends back (a lineup, a trade, a draft
pick) must still carry "LA".
"""

from __future__ import annotations

from joyce_ff.league import display, schema, scoring


def test_the_rams_are_shown_as_lar_and_nobody_else_changes():
    assert display.team("LA") == "LAR"
    assert display.team("LAC") == "LAC" and display.team("KC") == "KC"
    assert display.team(None) is None
    assert display.unit("LA", "DEF/ST") == "LAR DEF/ST"


def test_a_box_score_shows_lar_but_keeps_the_stored_code():
    conn = schema.connect(":memory:")
    schema.init_db(conn)
    schema.migrate(conn)
    conn.execute("INSERT INTO seasons(year,label,current_ff_week) VALUES (2026,'2026-27',1)")
    sid = conn.execute("SELECT id FROM seasons").fetchone()["id"]
    cid = conn.execute("INSERT INTO conferences(code,name) VALUES ('BLUE','Blue')").lastrowid
    tid = conn.execute("INSERT INTO teams(season_id,conference_id,name,team_number) "
                       "VALUES (?,?,'Aces',1)", (sid, cid)).lastrowid
    conn.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,"
                 "asset_ref,unit_type) VALUES (?,?,1,'QB','TEAM_UNIT','LA','QB')", (sid, tid))
    conn.commit()
    row = scoring.box_score(conn, sid, 1, tid)[0]
    # the box score prints the slot in its own column, so the unit is its club
    assert row["display"] == "LAR" and row["asset_ref"] == "LA"
