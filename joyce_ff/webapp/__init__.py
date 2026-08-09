"""
Flask application for stevejoyceff.com.

Thin HTTP layer over the league package: it authenticates a passcode and calls
the rule-enforcing repository. All league rules live in joyce_ff.league, not
here — routes stay dumb on purpose.

    from joyce_ff.webapp import create_app
    app = create_app()            # serves data/league.sqlite
"""

from __future__ import annotations

from flask import Flask, g, jsonify, render_template_string, request

from ..league import auth, repo, schema
from ..league import standings as st

DASHBOARD = """<!doctype html><meta charset=utf-8><title>Steve Joyce FF</title>
<style>body{font:15px system-ui;margin:2rem;max-width:900px}h1{margin:0}
table{border-collapse:collapse;width:100%;margin:.5rem 0}td,th{padding:4px 8px;border-bottom:1px solid #ccc;text-align:left}
.dim{color:#777}</style>
<h1>🏈 Steve Joyce Fantasy Football</h1>
<p class=dim>{{ season.label }} · week {{ season.current_ff_week }} · {{ nteams }} teams · live at stevejoyceff.com</p>
{% for conf, rows in standings.items() %}
<h3>{{ conf }} Conference</h3>
<table><tr><th>#</th><th>Team</th><th>W-L-T</th><th>Conf</th><th>PF</th><th>PA</th></tr>
{% for t in rows %}<tr><td>{{ t.seed }}{% if t.playoffs %}*{% endif %}</td><td>{{ t.name }}</td>
<td>{{ t.wins }}-{{ t.losses }}-{{ t.ties }}</td><td>{{ t.conf_wins }}-{{ t.conf_losses }}</td>
<td>{{ '%.0f'|format(t.pf) }}</td><td>{{ '%.0f'|format(t.pa) }}</td></tr>{% endfor %}</table>
{% endfor %}
<p class=dim>💀 Elimination pool — alive {{ pool.alive|length }} / {{ nteams }} ·
eliminated {{ pool.eliminated|length }}</p>
<p class=dim>* top 8 make the playoffs. (Mock UI ports next; this confirms live data + API.)</p>
"""


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path or schema.DEFAULT_DB_PATH)

    def db():
        if "db" not in g:
            g.db = schema.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def season():
        return db().execute(
            "SELECT id, label, current_ff_week FROM seasons ORDER BY year DESC LIMIT 1").fetchone()

    def _passcode():
        body = request.get_json(silent=True) or {}
        return body.get("passcode") or request.form.get("passcode") or ""

    def _team_authed(team_id) -> bool:
        pc = _passcode()
        return auth.check_team_passcode(db(), team_id, pc) or auth.is_commissioner(db(), pc)

    def _week(default):
        body = request.get_json(silent=True) or {}
        return int(body.get("week") or request.values.get("week") or default)

    # ---- pages ----
    @app.get("/")
    def dashboard():
        s = season()
        conn = db()
        n = conn.execute("SELECT COUNT(*) c FROM teams WHERE season_id=?", (s["id"],)).fetchone()["c"]
        return render_template_string(
            DASHBOARD, season=s, nteams=n,
            standings=st.compute_standings(conn, s["id"], s["current_ff_week"]),
            pool=st.pool_status(conn, s["id"]))

    @app.get("/healthz")
    def health():
        return {"ok": True}

    # ---- read API ----
    @app.get("/api/team/<int:team_id>/available")
    def available(team_id):
        conn = db()
        s = season()
        pos = request.args.get("position", "RB")
        conf = conn.execute("SELECT conference_id FROM teams WHERE id=?", (team_id,)).fetchone()
        if not conf:
            return jsonify(error="unknown team"), 404
        if pos in repo.INDIVIDUAL_POS:
            data = repo.available_players(conn, s["id"], conf["conference_id"], pos)
        else:
            data = repo.available_units(conn, s["id"], conf["conference_id"], pos)
        return jsonify(position=pos, available=data)

    # ---- write API (passcode-gated) ----
    def _guard(team_id):
        if not _team_authed(team_id):
            return jsonify(error="wrong passcode for this team"), 403
        return None

    @app.post("/api/team/<int:team_id>/trade")
    def trade(team_id):
        if (bad := _guard(team_id)):
            return bad
        b = request.get_json(force=True)
        try:
            tx = repo.do_trade(db(), season()["id"], team_id, b["position"],
                               b["out"], b["in"], _week(season()["current_ff_week"]))
        except repo.RuleError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, transaction_id=tx)

    @app.post("/api/team/<int:team_id>/open")
    def open_(team_id):
        if (bad := _guard(team_id)):
            return bad
        b = request.get_json(force=True)
        try:
            tx = repo.do_open(db(), season()["id"], team_id, b["position"],
                              b["out"], b["in"], _week(season()["current_ff_week"]))
        except repo.RuleError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, transaction_id=tx)

    @app.post("/api/team/<int:team_id>/lineup")
    def lineup(team_id):
        if (bad := _guard(team_id)):
            return bad
        b = request.get_json(force=True)
        try:
            repo.set_lineup(db(), season()["id"], team_id,
                            _week(season()["current_ff_week"]), b["starters"])
        except repo.RuleError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    @app.post("/api/team/<int:team_id>/payment")
    def payment(team_id):
        # commissioner-only
        if not auth.is_commissioner(db(), _passcode()):
            return jsonify(error="commissioner passcode required"), 403
        b = request.get_json(force=True)
        repo.record_payment(db(), season()["id"], team_id, int(b["amount_cents"]),
                            note=b.get("note"), actor="commissioner")
        return jsonify(ok=True, balance=repo.fee_balance_cents(db(), team_id))

    return app
