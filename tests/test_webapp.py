"""End-to-end tests of the Flask layer: passcode gating + repo wiring."""

import pytest

from joyce_ff.league import auth, schema
from joyce_ff.webapp import create_app


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "league.sqlite"
    conn = schema.connect(db)
    schema.init_db(conn)
    sid = schema.seed_reference(conn)
    conn.execute("INSERT INTO nfl_teams(season_id,abbr,name,bye_ff_week) VALUES (?,?,?,?)",
                 (sid, "ATL", "Atlanta", 7))
    conn.execute("INSERT INTO nfl_teams(season_id,abbr,name,bye_ff_week) VALUES (?,?,?,?)",
                 (sid, "DEN", "Denver", 9))
    conn.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr) "
                 "VALUES (?,?,?,?,?)", (sid, "p_bijan", "Bijan Robinson", "RB", "ATL"))
    conn.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr) "
                 "VALUES (?,?,?,?,?)", (sid, "p_warren", "Jaylen Warren", "RB", "DEN"))
    otb = conn.execute("SELECT id FROM teams WHERE name='OT Blitz'").fetchone()["id"]
    conn.execute("INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,"
                 "roster_slot,acquired_ff_week,acquired_via,created_at) "
                 "VALUES (?,?,'PLAYER','p_bijan','RB',1,'DRAFT','t')", (sid, otb))
    conn.commit()
    auth.set_team_passcode(conn, otb, "otblitz")
    auth.set_admin_passcode(conn, "Steve", "commish")
    conn.close()
    app = create_app(db)
    app.config.update(TESTING=True)
    c = app.test_client()
    c.otb = otb
    c.dbpath = str(db)
    return c


def test_dashboard_and_health(client):
    assert client.get("/").status_code == 200
    assert client.get("/healthz").get_json() == {"ok": True}


def test_otblitz_private_board_is_gated(client):
    assert client.get("/otblitz").status_code == 200          # shell page is public
    assert client.get("/api/otblitz/board").status_code == 403  # data is locked (no passcode set)
    conn = schema.connect(client.dbpath)
    auth.set_platform_passcode(conn, "sauce")
    conn.close()
    assert client.get("/api/otblitz/board?pc=wrong").status_code == 403
    # right passcode passes the gate (404 only because no board cache exists in the test)
    assert client.get("/api/otblitz/board?pc=sauce").status_code in (200, 404)


def test_available_endpoint(client):
    r = client.get(f"/api/team/{client.otb}/available?position=RB")
    refs = {p["gsis_id"] for p in r.get_json()["available"]}
    assert "p_warren" in refs and "p_bijan" not in refs   # bijan is owned


def test_trade_requires_correct_passcode(client):
    r = client.post(f"/api/team/{client.otb}/trade",
                    json={"passcode": "nope", "position": "RB", "out": "p_bijan", "in": "p_warren"})
    assert r.status_code == 403


def test_trade_rule_error_is_400_with_message(client):
    r = client.post(f"/api/team/{client.otb}/trade",
                    json={"passcode": "otblitz", "position": "RB", "out": "p_warren", "in": "p_bijan"})
    assert r.status_code == 400 and "don't own" in r.get_json()["error"]


def test_valid_trade_succeeds(client):
    r = client.post(f"/api/team/{client.otb}/trade",
                    json={"passcode": "otblitz", "position": "RB", "out": "p_bijan", "in": "p_warren"})
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_create_app_honors_env_db_path(tmp_path, monkeypatch):
    db = tmp_path / "env.sqlite"
    conn = schema.connect(db)
    schema.init_db(conn)
    schema.seed_reference(conn)
    conn.close()
    monkeypatch.setenv("JOYCE_DB_PATH", str(db))
    app = create_app()
    assert app.config["DB_PATH"] == str(db)
    with app.test_client() as c:
        assert c.get("/healthz").status_code == 200


def test_wsgi_module_exposes_app():
    import wsgi
    assert wsgi.app is not None


def test_state_season_selection(client):
    conn = schema.connect(client.dbpath)
    old = conn.execute("SELECT id, year FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
    sid2 = schema.seed_reference(conn, year=old["year"] + 1, label="next")
    conn.commit()
    conn.close()
    d = client.get("/api/state").get_json()
    assert d["season"]["id"] == sid2                       # newest season by default
    assert len(d["season"]["seasons"]) == 2
    d_old = client.get(f"/api/state?season={old['id']}").get_json()
    assert d_old["season"]["id"] == old["id"]              # ?season selects a prior year


def test_state_has_lineup_summary_and_alive_flag(client):
    d = client.get("/api/state").get_json()
    # current-week lineup-submission summary drives flags + commissioner view
    lu = d["lineups"]
    assert set(lu) >= {"week", "in", "total", "not_in"}
    assert lu["in"] == 0                         # no lineups set in this fixture
    assert lu["total"] >= 1 and len(lu["not_in"]) == lu["total"]
    # regression: standings rows must carry `alive` — without it the UI applied
    # the `dead` class (opacity .45) to every row and dimmed the whole table.
    row = d["standings"]["BLUE"][0]
    assert row["alive"] is True and "team_number" in row


def test_admin_score_override(client):
    bad = client.post(f"/api/admin/team/{client.otb}/score",
                      json={"passcode": "otblitz", "week": 1, "points": 99})
    assert bad.status_code == 403                                    # team passcode not enough
    ok = client.post(f"/api/admin/team/{client.otb}/score",
                     json={"passcode": "commish", "week": 1, "points": 99})
    assert ok.status_code == 200
    conn = schema.connect(client.dbpath)
    r = conn.execute("SELECT computed_points, adjusted FROM team_week_scores "
                     "WHERE team_id=? AND ff_week=1", (client.otb,)).fetchone()
    conn.close()
    assert r["computed_points"] == 99 and r["adjusted"] == 1


def test_only_commissioner_edits_other_weeks(client):
    cur = client.get("/api/state").get_json()["season"]["current"]
    mgr = client.post(f"/api/team/{client.otb}/lineup",
                      json={"passcode": "otblitz", "week": cur + 1, "starters": []})
    assert mgr.status_code == 400 and "commissioner" in mgr.get_json()["error"]
    # commissioner passes the week gate (then fails on the empty roster, a different error)
    comm = client.post(f"/api/team/{client.otb}/lineup",
                       json={"passcode": "commish", "week": cur + 1, "starters": []})
    assert comm.status_code == 400 and "commissioner can change" not in comm.get_json().get("error", "")


def test_admin_endpoints_require_commissioner(client):
    # team passcode is NOT enough for admin
    bad = client.post(f"/api/admin/team/{client.otb}",
                      json={"passcode": "otblitz", "manager_names": "X"})
    assert bad.status_code == 403
    ok = client.post(f"/api/admin/team/{client.otb}",
                     json={"passcode": "commish", "manager_names": "Scott & Drew"})
    assert ok.status_code == 200


def test_an_open_reports_who_it_covers_for_the_roster_lineup_and_box_score(client):
    r = client.post(f"/api/team/{client.otb}/open",
                    json={"passcode": "commish", "position": "RB", "out": "p_bijan",
                          "in": "p_warren", "week": 7})                   # Bijan's bye week
    assert r.status_code == 200, r.get_json()
    d = client.get(f"/api/team/{client.otb}/detail?week=7").get_json()
    (o,) = d["opens"]
    assert (o["name"], o["team"], o["covering"], o["covering_name"], o["covering_short"]) == \
        ("Jaylen Warren", "DEN", "p_bijan", "Bijan Robinson", "Robinson")


def test_a_player_traded_in_for_someone_who_already_played_is_flagged(client):
    """His replacement can't start this week, so Set Lineup can dim the button
    and say why instead of only rejecting the submit (Scott, 2026-09-18)."""
    conn = schema.connect(client.dbpath)
    sid = conn.execute("SELECT id FROM seasons ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO nfl_week_games(season_id,ff_week,game_id,home_team,away_team,"
                 "kickoff,final) VALUES (?,3,'g3','ATL','DEN','2026-09-20T12:00:00-05:00',1)", (sid,))
    conn.commit()
    conn.close()
    r = client.post(f"/api/team/{client.otb}/trade",      # commissioner: files against week 3
                    json={"passcode": "commish", "position": "RB", "out": "p_bijan",
                          "in": "p_warren", "week": 3})
    assert r.status_code == 200, r.get_json()
    d = client.get(f"/api/team/{client.otb}/detail?week=3").get_json()
    (came_in,) = [e for e in d["roster"] if e["asset_ref"] == "p_warren"]
    assert came_in["blocked_by"] == "Bijan Robinson"


def test_the_commissioner_can_see_every_move_of_the_season(client):
    """The main feed only keeps recent moves; this one keeps all of them, with
    the running count against the five free ones (Scott, 2026-09-18)."""
    conn = schema.connect(client.dbpath)
    sid = conn.execute("SELECT id FROM seasons ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr) "
                 "VALUES (?,'p_hall','Breece Hall','RB','ATL')", (sid,))
    conn.commit()
    conn.close()
    for out, inn in (("p_bijan", "p_warren"), ("p_warren", "p_hall")):
        r = client.post(f"/api/team/{client.otb}/trade",
                        json={"passcode": "commish", "position": "RB", "out": out,
                              "in": inn, "week": 3})
        assert r.status_code == 200, r.get_json()
    assert client.post("/api/admin/moves", json={"passcode": "otblitz"}).status_code == 403
    mv = client.post("/api/admin/moves", json={"passcode": "commish"}).get_json()["moves"]
    assert [(m["week"], m["team"], m["free_n"]) for m in mv] ==         [(3, "OT Blitz", 2), (3, "OT Blitz", 1)]            # newest first, counted oldest first


def test_pins_open_by_conference_and_stay_open_until_each_manager_sets_one(client):
    """At the Blue draft only Blue teams open; a team closes itself once its PIN
    is set, and the commissioner can close or open a single team (2026-09-16)."""
    conn = schema.connect(client.dbpath)
    blue = conn.execute("SELECT t.id FROM teams t JOIN conferences c ON c.id=t.conference_id "
                        "WHERE c.code='BLUE' AND t.name<>'OT Blitz' ORDER BY t.id").fetchall()
    red = conn.execute("SELECT t.id FROM teams t JOIN conferences c ON c.id=t.conference_id "
                       "WHERE c.code='RED' ORDER BY t.id").fetchall()
    conn.close()
    b1, b2, r1 = blue[0]["id"], blue[1]["id"], red[0]["id"]
    claim = lambda tid, pin: client.post(f"/api/team/{tid}/claim-pin", json={"pin": pin}).status_code

    assert claim(b1, "1234") == 400                                   # nothing open yet
    assert client.post("/api/admin/pins/open", json={"passcode": "otblitz", "conf": "BLUE"}).status_code == 403
    r = client.post("/api/admin/pins/open", json={"passcode": "commish", "conf": "BLUE"})
    assert r.status_code == 200 and r.get_json()["opened"] == 10      # OT Blitz already has a PIN
    assert claim(r1, "1234") == 400                                   # Red stays closed
    assert claim(b1, "1234") == 200
    assert claim(b1, "9999") == 400                                   # closed itself once set

    assert client.post(f"/api/admin/team/{b2}/close-pin", json={"passcode": "commish"}).status_code == 200
    assert claim(b2, "2468") == 400
    assert client.post(f"/api/admin/team/{r1}/open-pin", json={"passcode": "commish"}).status_code == 200
    assert claim(r1, "1357") == 200
    assert client.post(f"/api/admin/team/{client.otb}/open-pin",
                       json={"passcode": "commish"}).status_code == 400   # has a PIN: use Reset
    client.post("/api/admin/pins/open", json={"passcode": "commish", "conf": "BLUE"})
    client.post("/api/admin/pins/close", json={"passcode": "commish"})
    assert claim(b2, "2468") == 400


def test_a_pin_reset_lets_only_that_manager_set_a_new_pin_once(client):
    assert client.post(f"/api/admin/team/{client.otb}/reset-pin",
                       json={"passcode": "otblitz"}).status_code == 403   # commissioner only
    assert client.post(f"/api/admin/team/{client.otb}/reset-pin",
                       json={"passcode": "commish"}).status_code == 200
    trade = {"position": "RB", "out": "p_bijan", "in": "p_warren"}
    assert client.post(f"/api/team/{client.otb}/trade",
                       json={"passcode": "otblitz", **trade}).status_code == 403  # old PIN is dead
    conn = schema.connect(client.dbpath)
    other = conn.execute("SELECT id FROM teams WHERE name='Pike'").fetchone()["id"]
    conn.close()
    assert client.post(f"/api/team/{other}/claim-pin", json={"pin": "1111"}).status_code == 400
    assert client.post(f"/api/team/{client.otb}/claim-pin", json={"pin": "2468"}).status_code == 200
    assert client.post(f"/api/team/{client.otb}/claim-pin", json={"pin": "9999"}).status_code == 400
    assert client.post(f"/api/team/{client.otb}/trade",
                       json={"passcode": "2468", **trade}).status_code == 200


def test_payment_is_commissioner_only(client):
    bad = client.post(f"/api/team/{client.otb}/payment",
                      json={"passcode": "otblitz", "amount_cents": 200})
    assert bad.status_code == 403
    ok = client.post(f"/api/team/{client.otb}/payment",
                     json={"passcode": "commish", "amount_cents": 200})
    assert ok.status_code == 200


def test_a_box_score_keeps_nine_slots_with_an_extra_starter_in_the_slot_he_fills(client):
    """No QB started (his bye) and a 3rd RB in: the RB shows in the QB row,
    tagged with his real position and why (2026-09-16)."""
    conn = schema.connect(client.dbpath)
    sid = conn.execute("SELECT id FROM seasons").fetchone()["id"]
    rows = [("C", "TEAM_UNIT", "KC"), ("K", "TEAM_UNIT", "BAL"), ("DEF/ST", "TEAM_UNIT", "PIT"),
            ("RB", "PLAYER", "rb1"), ("RB", "PLAYER", "rb2"), ("RB", "PLAYER", "rb3"),
            ("R", "PLAYER", "w1"), ("R", "PLAYER", "w2"), ("R", "PLAYER", "w3")]
    for slot, kind, ref in rows:
        conn.execute("INSERT INTO weekly_lineups(season_id,team_id,ff_week,roster_slot,asset_kind,asset_ref,unit_type) "
                     "VALUES (?,?,3,?,?,?,?)", (sid, client.otb, slot, kind, ref, slot if kind == "TEAM_UNIT" else None))
    conn.commit()
    conn.close()
    box = client.get(f"/api/team/{client.otb}/detail?week=3").get_json()["box"]
    assert [x["slot_shown"] for x in box] == ["C", "K", "DEF/ST", "QB", "RB", "RB", "R", "R", "R"]
    qb_row = box[3]
    assert (qb_row["asset_ref"], qb_row["roster_slot"], qb_row["fills_as"]) == ("rb3", "RB", "RB")
    assert qb_row["flex_short"].startswith("for ") and "(bye)" in qb_row["flex_note"]


def test_rosters_page_lists_every_team_by_conference_with_the_player_pool(client):
    assert client.get("/rosters").status_code == 200
    d = client.get("/api/rosters").get_json()
    blue = {t["name"]: t for t in d["conferences"]["BLUE"]}
    assert len(d["conferences"]["BLUE"]) == 11 and len(d["conferences"]["RED"]) == 11
    names = [t["name"] for t in d["conferences"]["BLUE"]]
    assert names == sorted(names, key=str.lower)                            # alphabetical
    assert [p["ref"] for p in blue["OT Blitz"]["players"]] == ["p_bijan"]
    assert {"p_bijan", "p_warren"} <= {p["ref"] for p in d["pool"]}          # searchable, owned or not
    # a trade shows immediately
    client.post(f"/api/team/{client.otb}/trade",
                json={"passcode": "otblitz", "position": "RB", "out": "p_bijan", "in": "p_warren"})
    d = client.get("/api/rosters").get_json()
    ot = next(t for t in d["conferences"]["BLUE"] if t["name"] == "OT Blitz")
    assert [p["ref"] for p in ot["players"]] == ["p_warren"]
