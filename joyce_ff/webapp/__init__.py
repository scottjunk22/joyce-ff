"""
Flask application for stevejoyceff.com.

Thin HTTP layer over the league package: it authenticates a passcode and calls
the rule-enforcing repository. All league rules live in joyce_ff.league, not
here — routes stay dumb on purpose.

    from joyce_ff.webapp import create_app
    app = create_app()            # serves data/league.sqlite
"""

from __future__ import annotations

import os
import secrets

from flask import Flask, g, jsonify, render_template, request

from ..league import auth, repo, schema, scoring
from ..league import standings as st


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    # DB location: explicit arg > JOYCE_DB_PATH env (a persistent host path) >
    # the default. Secret key from env in production; ephemeral otherwise.
    app.config["DB_PATH"] = str(db_path or os.environ.get("JOYCE_DB_PATH")
                                or schema.DEFAULT_DB_PATH)
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)

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
        return render_template("dashboard.html")

    @app.get("/healthz")
    def health():
        return {"ok": True}

    def _dname(conn, sid, kind, ref, unit=None):
        if kind == "PLAYER":
            r = conn.execute("SELECT name FROM nfl_players WHERE season_id=? AND gsis_id=?",
                             (sid, ref)).fetchone()
            return r["name"] if r else ref
        return f"{ref} {unit or ''}".strip()

    # ---- read API ----
    @app.get("/api/state")
    def state():
        conn = db()
        s = season()
        sid, wk = s["id"], s["current_ff_week"]
        stand = st.compute_standings(conn, sid, wk)
        scores = {(r["team_id"]): r["computed_points"] for r in conn.execute(
            "SELECT team_id, computed_points FROM team_week_scores WHERE season_id=? AND ff_week=?",
            (sid, wk))}
        board = []
        for m in conn.execute(
            "SELECT m.id, m.kind, m.home_team_id h, m.away_team_id a, th.name hn, ta.name an "
            "FROM matchups m JOIN teams th ON th.id=m.home_team_id "
            "JOIN teams ta ON ta.id=m.away_team_id WHERE m.season_id=? AND m.ff_week=?",
                (sid, wk)):
            board.append({"kind": m["kind"],
                          "home": {"id": m["h"], "name": m["hn"], "points": scores.get(m["h"])},
                          "away": {"id": m["a"], "name": m["an"], "points": scores.get(m["a"])}})
        tx = {"BLUE": [], "RED": []}
        for r in conn.execute(
            "SELECT tr.ff_week w, t.name team, c.code conf, tr.type, tr.position, "
            "tr.out_asset_kind ok, tr.out_asset_ref oref, tr.in_asset_kind ik, tr.in_asset_ref iref "
            "FROM transactions tr JOIN teams t ON t.id=tr.team_id "
            "JOIN conferences c ON c.id=t.conference_id WHERE tr.season_id=? AND tr.reversed=0 "
            "ORDER BY tr.ff_week DESC, tr.id DESC LIMIT 20", (sid,)):
            tx[r["conf"]].append({"week": r["w"], "team": r["team"], "type": r["type"],
                "desc": f"{_dname(conn, sid, r['ok'], r['oref'])} → {_dname(conn, sid, r['ik'], r['iref'])}"})
        return jsonify(season={"label": s["label"], "week": wk},
                       standings=stand, scoreboard=board,
                       pool=st.pool_status(conn, sid), transactions=tx)

    @app.get("/api/team/<int:team_id>/detail")
    def team_detail(team_id):
        conn = db()
        s = season()
        sid, wk = s["id"], s["current_ff_week"]
        row = conn.execute("SELECT name, manager_names FROM teams WHERE id=?", (team_id,)).fetchone()
        roster = [{"slot": e["roster_slot"], "name": _dname(conn, sid, e["asset_kind"], e["asset_ref"], e["unit_type"]),
                   "asset_ref": e["asset_ref"]} for e in repo.current_roster(conn, team_id)]
        fees = repo.fee_balance_cents(conn, team_id)
        hist = [{"week": t["ff_week"], "type": t["type"],
                 "desc": f"{_dname(conn, sid, t['out_asset_kind'], t['out_asset_ref'])} → "
                         f"{_dname(conn, sid, t['in_asset_kind'], t['in_asset_ref'])}",
                 "fee": t["fee_cents"]} for t in repo.transaction_history(conn, team_id)]
        return jsonify(name=row["name"], managers=row["manager_names"],
                       roster=roster, fees=fees, history=hist,
                       box=scoring.box_score(conn, sid, wk, team_id))

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
