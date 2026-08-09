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


def test_admin_endpoints_require_commissioner(client):
    # team passcode is NOT enough for admin
    bad = client.post(f"/api/admin/team/{client.otb}",
                      json={"passcode": "otblitz", "manager_names": "X"})
    assert bad.status_code == 403
    ok = client.post(f"/api/admin/team/{client.otb}",
                     json={"passcode": "commish", "manager_names": "Scott & Drew"})
    assert ok.status_code == 200


def test_admin_can_set_team_passcode(client):
    r = client.post(f"/api/admin/team/{client.otb}/passcode",
                    json={"passcode": "commish", "new_passcode": "newpc"})
    assert r.status_code == 200
    # the new passcode now authorizes a team action
    t = client.post(f"/api/team/{client.otb}/trade",
                    json={"passcode": "newpc", "position": "RB", "out": "p_bijan", "in": "p_warren"})
    assert t.status_code == 200


def test_payment_is_commissioner_only(client):
    bad = client.post(f"/api/team/{client.otb}/payment",
                      json={"passcode": "otblitz", "amount_cents": 200})
    assert bad.status_code == 403
    ok = client.post(f"/api/team/{client.otb}/payment",
                     json={"passcode": "commish", "amount_cents": 200})
    assert ok.status_code == 200
