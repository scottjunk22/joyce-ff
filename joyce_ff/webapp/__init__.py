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

    # Bring an existing DB up to the current schema (e.g. adds roster slot_order
    # on the live host, which was created before that column existed).
    _mig = schema.connect(app.config["DB_PATH"])
    try:
        schema.migrate(_mig)
    finally:
        _mig.close()

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

    def _season_sel():
        """(selected season row, all season rows newest-first). Selection comes
        from ?season=<id>; defaults to the newest season."""
        rows = list(db().execute(
            "SELECT id, year, label, current_ff_week FROM seasons ORDER BY year DESC"))
        want = request.values.get("season")
        if want:
            for r in rows:
                if r["id"] == int(want):
                    return r, rows
        return rows[0], rows

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

    def _asset_team(conn, sid, kind, ref):
        if kind == "TEAM_UNIT":
            return ref
        r = conn.execute("SELECT nfl_team_abbr FROM nfl_players WHERE season_id=? AND gsis_id=?",
                         (sid, ref)).fetchone()
        return r["nfl_team_abbr"] if r else None

    def _tx_parts(conn, sid, pos, ok, oref, ik, iref):
        """Structured transaction line: position + each side's name and (for
        individual players) NFL team, so the UI can render position/team tags."""
        def side(kind, ref):
            d = {"name": _dname(conn, sid, kind, ref), "kind": kind}
            if kind == "PLAYER":
                d["team"] = _asset_team(conn, sid, kind, ref)
            return d
        return {"pos": pos, "out": side(ok, oref), "in": side(ik, iref)}

    def _pending_teams(conn, sid, wk):
        """NFL teams that HAVE a game this FF week that isn't final yet (i.e.
        'still to play'). Excludes bye teams (no game) and finished teams."""
        from ..data_sources import nflverse as nv
        year = conn.execute("SELECT year FROM seasons WHERE id=?", (sid,)).fetchone()["year"]
        nflw = scoring.nfl_week_for(conn, sid, wk)
        g = nv.load_games()
        g = g[(g["season"] == year) & (g["week"] == nflw)]
        playing = set(g["home_team"]) | set(g["away_team"])
        gf = g[g["home_score"].notna()]
        final = set(gf["home_team"]) | set(gf["away_team"])
        return playing - final

    # ---- read API ----
    @app.get("/api/state")
    def state():
        conn = db()
        s, all_seasons = _season_sel()
        sid = s["id"]
        wk = int(request.args.get("week") or s["current_ff_week"])
        stand = st.compute_standings(conn, sid, wk)
        # compute_standings returns records only; merge in per-team metadata the
        # UI needs (alive dimming, commissioner # / slot fields).
        meta = {r["id"]: r for r in conn.execute(
            "SELECT id, alive, eliminated_ff_week, team_number, draft_slot "
            "FROM teams WHERE season_id=?", (sid,))}
        for cc in ("BLUE", "RED"):
            for t in stand[cc]:
                m = meta.get(t["team_id"])
                if m:
                    t["alive"] = bool(m["alive"])
                    t["eliminated_ff_week"] = m["eliminated_ff_week"]
                    t["team_number"] = m["team_number"]
                    t["draft_slot"] = m["draft_slot"]
        scores = {r["team_id"]: r["computed_points"] for r in conn.execute(
            "SELECT team_id, computed_points FROM team_week_scores WHERE season_id=? AND ff_week=?",
            (sid, wk))}
        try:
            pending = _pending_teams(conn, sid, wk)
        except Exception:
            pending = set()                    # unknown -> treat as all done
        toplay, lset = {}, {}
        for r in conn.execute("SELECT team_id, asset_kind, asset_ref FROM weekly_lineups "
                              "WHERE season_id=? AND ff_week=?", (sid, wk)):
            lset[r["team_id"]] = lset.get(r["team_id"], 0) + 1
            abbr = _asset_team(conn, sid, r["asset_kind"], r["asset_ref"])
            if abbr and abbr in pending:
                toplay[r["team_id"]] = toplay.get(r["team_id"], 0) + 1

        def side(tid, name):
            return {"id": tid, "name": name, "points": scores.get(tid),
                    "to_play": toplay.get(tid, 0), "lineup_set": lset.get(tid, 0) >= 9}
        board = []
        for m in conn.execute(
            "SELECT m.kind, m.home_team_id h, m.away_team_id a, th.name hn, ta.name an "
            "FROM matchups m JOIN teams th ON th.id=m.home_team_id "
            "JOIN teams ta ON ta.id=m.away_team_id WHERE m.season_id=? AND m.ff_week=?", (sid, wk)):
            board.append({"kind": m["kind"], "home": side(m["h"], m["hn"]),
                          "away": side(m["a"], m["an"])})

        tx = {"BLUE": [], "RED": []}
        for r in conn.execute(
            "SELECT tr.ff_week w, t.name team, c.code conf, tr.type, tr.position pos, "
            "tr.out_asset_kind ok, tr.out_asset_ref oref, tr.in_asset_kind ik, tr.in_asset_ref iref "
            "FROM transactions tr JOIN teams t ON t.id=tr.team_id "
            "JOIN conferences c ON c.id=t.conference_id WHERE tr.season_id=? AND tr.reversed=0 "
            "ORDER BY tr.ff_week DESC, tr.id DESC LIMIT 20", (sid,)):
            tx[r["conf"]].append({"week": r["w"], "team": r["team"], "type": r["type"],
                **_tx_parts(conn, sid, r["pos"], r["ok"], r["oref"], r["ik"], r["iref"])})

        fees = {t["team_id"]: repo.fee_balance_cents(conn, t["team_id"])
                for conf in ("BLUE", "RED") for t in stand[conf]}
        pool = {"alive": [], "eliminated": []}
        for r in conn.execute("SELECT name, eliminated_ff_week e FROM teams WHERE season_id=? ORDER BY e, name", (sid,)):
            if r["e"] is not None and r["e"] <= wk:
                pool["eliminated"].append({"name": r["name"], "eliminated_ff_week": r["e"]})
            else:
                pool["alive"].append({"name": r["name"]})
        byes = [r["abbr"] for r in conn.execute(
            "SELECT abbr FROM nfl_teams WHERE season_id=? AND bye_ff_week=? ORDER BY abbr",
            (sid, wk))]
        weeks = [r["w"] for r in conn.execute(
            "SELECT DISTINCT ff_week w FROM team_week_scores WHERE season_id=? ORDER BY ff_week", (sid,))] or [wk]
        last = conn.execute("SELECT MAX(computed_at) c FROM team_week_scores WHERE season_id=?", (sid,)).fetchone()["c"]

        # Lineup-submission status for the CURRENT week (independent of the
        # viewed week) — drives the straggler flags + commissioner summary.
        cur = s["current_ff_week"]
        alive_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM teams WHERE season_id=? AND alive=1", (sid,))}
        cur_counts = {r["team_id"]: r["c"] for r in conn.execute(
            "SELECT team_id, COUNT(*) c FROM weekly_lineups WHERE season_id=? AND ff_week=? "
            "GROUP BY team_id", (sid, cur))}
        lin_in, lin_notin = 0, []
        for cc in ("BLUE", "RED"):
            for t in stand[cc]:
                if t["team_id"] not in alive_ids:
                    continue                        # eliminated teams don't set lineups
                if cur_counts.get(t["team_id"], 0) >= 9:
                    lin_in += 1
                else:
                    lin_notin.append({"id": t["team_id"], "name": t["name"]})
        lineups = {"week": cur, "in": lin_in, "total": lin_in + len(lin_notin),
                   "not_in": lin_notin}

        return jsonify(season={"id": sid, "year": s["year"], "label": s["label"], "week": wk,
                               "current": s["current_ff_week"], "weeks": weeks, "last_updated": last,
                               "seasons": [{"id": r["id"], "year": r["year"], "label": r["label"]}
                                           for r in all_seasons]},
                       standings=stand, scoreboard=board, fees=fees, pool=pool,
                       transactions=tx, lineups=lineups, byes=byes)

    @app.get("/api/team/<int:team_id>/detail")
    def team_detail(team_id):
        conn = db()
        s, _ = _season_sel()
        sid = s["id"]
        wk = int(request.args.get("week") or s["current_ff_week"])
        row = conn.execute("SELECT name, manager_names FROM teams WHERE id=?", (team_id,)).fetchone()
        byes = {r["abbr"] for r in conn.execute(
            "SELECT abbr FROM nfl_teams WHERE season_id=? AND bye_ff_week=?", (sid, wk))}
        roster = []
        for e in repo.current_roster(conn, team_id):
            team = _asset_team(conn, sid, e["asset_kind"], e["asset_ref"])
            roster.append({"slot": e["roster_slot"],
                           "name": _dname(conn, sid, e["asset_kind"], e["asset_ref"], e["unit_type"]),
                           "asset_ref": e["asset_ref"], "kind": e["asset_kind"],
                           "team": team, "bye": team in byes})
        fees = repo.fee_balance_cents(conn, team_id)
        hist = [{"week": t["ff_week"], "type": t["type"], "fee": t["fee_cents"],
                 **_tx_parts(conn, sid, t["position"], t["out_asset_kind"], t["out_asset_ref"],
                             t["in_asset_kind"], t["in_asset_ref"])}
                for t in repo.transaction_history(conn, team_id)]
        opens = [{"asset_ref": t["in_asset_ref"], "position": t["position"],
                  "name": _dname(conn, sid, t["in_asset_kind"], t["in_asset_ref"])}
                 for t in conn.execute(
                     "SELECT in_asset_ref, in_asset_kind, position FROM transactions "
                     "WHERE season_id=? AND team_id=? AND ff_week=? AND type='OPEN' AND reversed=0",
                     (sid, team_id, wk))]
        pays = [{"amount_cents": p["amount_cents"], "note": p["note"], "at": p["applied_at"]}
                for p in conn.execute(
                    "SELECT amount_cents, note, applied_at FROM payments WHERE team_id=? "
                    "ORDER BY id", (team_id,))]
        return jsonify(name=row["name"], managers=row["manager_names"],
                       roster=roster, fees=fees, history=hist, payments=pays, opens=opens,
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
        sid, wk = season()["id"], _week(season()["current_ff_week"])
        try:
            from ..league.locks import locked_assets
            locked = locked_assets(db(), sid, wk)
            repo.set_lineup(db(), sid, team_id, wk, b["starters"], locked_refs=locked)
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

    # ---- commissioner admin (commissioner passcode only) ----
    def _commish():
        return None if auth.is_commissioner(db(), _passcode()) else \
            (jsonify(error="commissioner passcode required"), 403)

    @app.post("/api/admin/team/<int:team_id>")
    def admin_set_team(team_id):
        if (bad := _commish()):
            return bad
        b = request.get_json(force=True)
        for col in ("team_number", "draft_slot", "manager_names"):
            if col in b and b[col] not in (None, ""):
                val = int(b[col]) if col != "manager_names" else b[col]
                try:
                    db().execute(f"UPDATE teams SET {col}=? WHERE id=?", (val, team_id))
                except Exception as e:  # e.g. UNIQUE team_number collision
                    return jsonify(error=f"{col}: {e}"), 400
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/admin/team/<int:team_id>/passcode")
    def admin_set_passcode(team_id):
        if (bad := _commish()):
            return bad
        new = request.get_json(force=True).get("new_passcode")
        if not new:
            return jsonify(error="new_passcode required"), 400
        auth.set_team_passcode(db(), team_id, new)
        return jsonify(ok=True)

    @app.post("/api/admin/run-current")
    def admin_run_current():
        if (bad := _commish()):
            return bad
        from ..league.runner import run_current
        return jsonify(ok=True, scored_weeks=run_current(db(), season()["id"]))

    @app.post("/api/admin/reverse/<int:tx_id>")
    def admin_reverse(tx_id):
        if (bad := _commish()):
            return bad
        db().execute("UPDATE transactions SET reversed=1 WHERE id=?", (tx_id,))
        db().commit()
        return jsonify(ok=True)

    return app
