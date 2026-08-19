"""
Flask application for stevejoyceff.com.

Thin HTTP layer over the league package: it authenticates a passcode and calls
the rule-enforcing repository. All league rules live in joyce_ff.league, not
here — routes stay dumb on purpose.

    from joyce_ff.webapp import create_app
    app = create_app()            # serves data/league.sqlite
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request

from ..league import auth, repo, schema, scoring
from ..league import standings as st

# Everything the app serves is mirrored under this prefix against a separate
# database — the practice universe. See create_app().
DARK_PREFIX = "/dark"


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    # DB location: explicit arg > JOYCE_DB_PATH env (a persistent host path) >
    # the default. Secret key from env in production; ephemeral otherwise.
    app.config["DB_PATH"] = str(db_path or os.environ.get("JOYCE_DB_PATH")
                                or schema.DEFAULT_DB_PATH)
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
    # The practice ("dark") universe: the same site served under /dark, backed by
    # a SEPARATE database file. Full isolation is the point — a dry run can be
    # drafted, scored and wiped without the real league ever seeing it.
    app.config["DARK_DB_PATH"] = str(Path(app.config["DB_PATH"]).with_name("league_dark.sqlite"))

    # Bring an existing DB up to the current schema (e.g. adds roster slot_order
    # on the live host, which was created before that column existed).
    _mig = schema.connect(app.config["DB_PATH"])
    try:
        schema.migrate(_mig)
    finally:
        _mig.close()

    def _is_dark() -> bool:
        return (request.path or "").startswith(DARK_PREFIX)

    def _ensure_dark_db(path: str) -> None:
        """Create the practice DB on first use: same schema, same passcodes as
        the real site (copied, never re-typed), but no seasons or teams — the
        commissioner's 'Start new season' builds those."""
        fresh = not Path(path).exists()
        conn = schema.connect(path)
        try:
            schema.init_db(conn)
            schema.migrate(conn)
            if fresh:
                for code, name in (("BLUE", "Blue Conference"), ("RED", "Red Conference")):
                    conn.execute("INSERT OR IGNORE INTO conferences(code, name) VALUES (?,?)",
                                 (code, name))
                src = schema.connect(app.config["DB_PATH"])
                try:
                    for r in src.execute("SELECT name, passcode_hash, created_at FROM admins"):
                        conn.execute("INSERT OR IGNORE INTO admins(name,passcode_hash,created_at) "
                                     "VALUES (?,?,?)", (r["name"], r["passcode_hash"], r["created_at"]))
                    for r in src.execute("SELECT key, value FROM settings WHERE key='otblitz_pc'"):
                        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                                     (r["key"], r["value"]))
                finally:
                    src.close()
                conn.commit()
        finally:
            conn.close()

    def db():
        dark = _is_dark()
        key = "db_dark" if dark else "db"
        if key not in g:
            if dark:
                if not app.config.get("_dark_ready"):
                    _ensure_dark_db(app.config["DARK_DB_PATH"])
                    app.config["_dark_ready"] = True
                setattr(g, key, schema.connect(app.config["DARK_DB_PATH"]))
            else:
                setattr(g, key, schema.connect(app.config["DB_PATH"]))
        return getattr(g, key)

    @app.teardown_appcontext
    def _close(_exc):
        for key in ("db", "db_dark"):
            conn = g.pop(key, None)
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
        if not rows:
            return None, []          # fresh league: no season created yet
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

    # ---- private OT-Blitz platform (Scott's eyes only) ----
    board_json = os.environ.get("JOYCE_BOARD_PATH") or str(
        Path(__file__).resolve().parents[2] / "data" / "boards.json")

    @app.get("/otblitz")
    def otblitz():
        return render_template("otblitz.html")

    @app.get("/api/otblitz/board")
    def otblitz_board():
        # gated by the private platform passcode — never the team/commish one
        if not auth.check_platform_passcode(db(), request.values.get("pc", "")):
            return jsonify(error="locked"), 403
        try:
            return jsonify(json.loads(Path(board_json).read_text(encoding="utf-8")))
        except FileNotFoundError:
            return jsonify(error="board not built yet — run: python manage.py board-cache"), 404

    def _platform(pc):
        return auth.check_platform_passcode(db(), pc)

    @app.get("/api/otblitz/draft")
    def otblitz_draft():
        if not _platform(request.values.get("pc", "")):
            return jsonify(error="locked"), 403
        conn, s = db(), season()
        sid = s["id"]
        code = (request.args.get("conf") or "BLUE").upper()
        conf = conn.execute("SELECT id FROM conferences WHERE code=?", (code,)).fetchone()
        if not conf:
            return jsonify(error="unknown conference"), 400
        team_rows = conn.execute(
            "SELECT id, name, draft_slot FROM teams WHERE season_id=? AND conference_id=? "
            "ORDER BY COALESCE(draft_slot, 99), name", (sid, conf["id"])).fetchall()
        teams, drafted_players, drafted_units = [], [], []
        for t in team_rows:
            roster = {sl: [] for sl in ("C", "K", "DEF/ST", "QB", "RB", "R")}
            for e in conn.execute(
                "SELECT id, asset_kind, asset_ref, unit_type, roster_slot FROM roster_entries "
                "WHERE team_id=? AND acquired_via='DRAFT' AND released_ff_week IS NULL ORDER BY id",
                    (t["id"],)):
                roster[e["roster_slot"]].append({
                    "id": e["id"], "ref": e["asset_ref"], "kind": e["asset_kind"],
                    "name": _dname(conn, sid, e["asset_kind"], e["asset_ref"], e["unit_type"])})
                if e["asset_kind"] == "PLAYER":
                    drafted_players.append(e["asset_ref"])
                else:
                    drafted_units.append(f"{e['asset_ref']}|{e['roster_slot']}")
            teams.append({"id": t["id"], "name": t["name"],
                          "draft_slot": t["draft_slot"], "roster": roster})
        from ..draft import order as do
        seq = do.build_sequence()
        # The clock is a persisted cursor, decoupled from roster edits. Lazily
        # initialize it from the picks already made (continuity for a draft that
        # began under the old count-based model).
        pick = repo.get_draft_cursor(conn, sid, conf["id"])
        if pick is None:
            pick = len(drafted_players) + len(drafted_units) + 1
            repo.set_draft_cursor(conn, sid, conf["id"], pick)
        slot_by = {t["draft_slot"]: t["id"] for t in team_rows if t["draft_slot"]}
        on_clock = slot_by.get(seq[pick - 1]["slot"]) if 1 <= pick <= len(seq) else None
        otb = next((t for t in team_rows if t["name"] == "OT Blitz"), None)
        otb_next = None
        if otb and otb["draft_slot"]:
            otb_next = next((o for o in range(pick, len(seq) + 1)
                             if seq[o - 1]["slot"] == otb["draft_slot"]), None)
        return jsonify(conf=code, teams=teams, drafted_players=drafted_players,
                       drafted_units=drafted_units, pick=pick, round=(pick - 1) // do.SLOTS + 1,
                       total_picks=len(seq), on_clock_team=on_clock,
                       otb_team=(otb["id"] if otb else None), otb_next=otb_next)

    def _conf_id(code):
        r = db().execute("SELECT id FROM conferences WHERE code=?", ((code or "BLUE").upper(),)).fetchone()
        return r["id"] if r else None

    @app.post("/api/otblitz/draft/pick")
    def otblitz_pick():
        if not _platform(_passcode()):
            return jsonify(error="locked"), 403
        b = request.get_json(force=True)
        conn, sid, team_id = db(), season()["id"], int(b["team_id"])
        conf_id = repo._team_conf(conn, team_id)
        # Capture the clock BEFORE inserting so advance = old clock + 1.
        prev = repo.get_draft_cursor(conn, sid, conf_id)
        if prev is None:
            prev = repo.draft_pick_count(conn, sid, conf_id) + 1
        try:
            repo.draft_player(conn, sid, team_id, b["kind"], b["ref"], b["slot"])
        except repo.RuleError as e:
            return jsonify(error=str(e)), 400
        if b.get("advance"):
            repo.set_draft_cursor(conn, sid, conf_id, prev + 1)
        elif repo.get_draft_cursor(conn, sid, conf_id) is None:
            repo.set_draft_cursor(conn, sid, conf_id, prev)  # persist lazy init
        return jsonify(ok=True)

    @app.post("/api/otblitz/draft/undo")
    def otblitz_undo():
        if not _platform(_passcode()):
            return jsonify(error="locked"), 403
        ref = repo.undo_last_draft(db(), season()["id"], _conf_id(request.get_json(force=True).get("conf")))
        return jsonify(ok=True, undone=ref)

    @app.post("/api/otblitz/draft/remove")
    def otblitz_remove_pick():
        if not _platform(_passcode()):
            return jsonify(error="locked"), 403
        b = request.get_json(force=True)
        ref = repo.remove_draft_entry(db(), season()["id"], _conf_id(b.get("conf")), int(b["entry_id"]))
        return jsonify(ok=True, removed=ref)

    @app.get("/api/otblitz/chat")
    def otblitz_chat_read():
        if not _platform(request.values.get("pc", "")):
            return jsonify(error="locked"), 403
        since = int(request.values.get("since") or 0)
        rows = db().execute(
            "SELECT id, author, body, created_at FROM chat_messages WHERE id>? "
            "ORDER BY id DESC LIMIT 200", (since,)).fetchall()
        msgs = [dict(r) for r in reversed(rows)]
        return jsonify(messages=msgs, last_id=(msgs[-1]["id"] if msgs else since))

    @app.post("/api/otblitz/chat")
    def otblitz_chat_send():
        if not _platform(_passcode()):
            return jsonify(error="locked"), 403
        b = request.get_json(force=True)
        author = (b.get("author") or "").strip()[:24]
        body = (b.get("body") or "").strip()[:2000]
        if not author or not body:
            return jsonify(error="author and body required"), 400
        cur = db().execute(
            "INSERT INTO chat_messages(author, body, created_at) VALUES (?,?,?)",
            (author, body, repo._now()))
        db().commit()
        return jsonify(ok=True, id=cur.lastrowid)

    @app.post("/api/otblitz/draft/slots")
    def otblitz_slots():
        if not _platform(_passcode()):
            return jsonify(error="locked"), 403
        b = request.get_json(force=True)
        conn, sid, conf_id = db(), season()["id"], _conf_id(b.get("conf"))
        if conf_id is None:
            return jsonify(error="unknown conference"), 400
        try:
            n = repo.set_draft_slots(conn, sid, conf_id, b.get("slots") or {})
        except repo.RuleError as e:
            return jsonify(error=str(e)), 400
        # Slots define the pick sequence, so a changed draw invalidates the clock.
        repo.set_draft_cursor(conn, sid, conf_id, repo.draft_pick_count(conn, sid, conf_id) + 1)
        return jsonify(ok=True, assigned=n)

    @app.post("/api/otblitz/draft/clock")
    def otblitz_clock():
        if not _platform(_passcode()):
            return jsonify(error="locked"), 403
        b = request.get_json(force=True)
        pick = repo.set_draft_cursor(db(), season()["id"], _conf_id(b.get("conf")), int(b["pick"]))
        return jsonify(ok=True, pick=pick)

    @app.post("/api/otblitz/draft/reset")
    def otblitz_reset():
        if not _platform(_passcode()):
            return jsonify(error="locked"), 403
        n = repo.reset_draft(db(), season()["id"], _conf_id(request.get_json(force=True).get("conf")))
        return jsonify(ok=True, cleared=n)

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

    def _tx_fee_info(tx_id, team_id):
        """What a just-made transaction actually cost + free trades left, so the
        UI can say 'free (2 left)' vs '$2 fee' instead of always '$2'."""
        fee = db().execute("SELECT fee_cents FROM transactions WHERE id=?",
                            (tx_id,)).fetchone()["fee_cents"]
        return {"fee_cents": fee, "free_left": repo.fee_balance_cents(db(), team_id)["free_left"]}

    def _account(conn, sid, team_id):
        """A team's money: fees owed/paid plus elimination-pool winnings, netted."""
        a = repo.fee_balance_cents(conn, team_id)
        a["winnings_cents"] = st.team_winnings_cents(conn, sid, team_id)
        a["net_cents"] = a["balance_cents"] - a["winnings_cents"]
        return a

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
        if s is None:
            # No season yet — a fresh league (and the practice universe before
            # its first "Start new season"). Return an empty-but-valid payload
            # so the page renders its setup state instead of erroring.
            return jsonify(season=None, standings={"BLUE": [], "RED": []}, scoreboard=[],
                           fees={}, pool={"alive": [], "eliminated": []},
                           transactions={"BLUE": [], "RED": []}, lineups=None,
                           byes=[], payout=None)
        sid = s["id"]
        wk = int(request.args.get("week") or s["current_ff_week"])
        stand = st.compute_standings(conn, sid, wk)
        # compute_standings returns records only; merge in per-team metadata the
        # UI needs (alive dimming, commissioner # / slot fields).
        meta = {r["id"]: r for r in conn.execute(
            "SELECT id, alive, eliminated_ff_week, team_number, draft_slot, manager_names, "
            "(passcode_hash IS NOT NULL) has_pin FROM teams WHERE season_id=?", (sid,))}
        for cc in ("BLUE", "RED"):
            for t in stand[cc]:
                m = meta.get(t["team_id"])
                if m:
                    t["alive"] = bool(m["alive"])
                    t["eliminated_ff_week"] = m["eliminated_ff_week"]
                    t["team_number"] = m["team_number"]
                    t["draft_slot"] = m["draft_slot"]
                    t["manager_names"] = m["manager_names"]
                    t["has_pin"] = bool(m["has_pin"])
        scores, adjusted = {}, set()
        for r in conn.execute("SELECT team_id, computed_points, adjusted FROM team_week_scores "
                              "WHERE season_id=? AND ff_week=?", (sid, wk)):
            scores[r["team_id"]] = r["computed_points"]
            if r["adjusted"]:
                adjusted.add(r["team_id"])
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
                    "to_play": toplay.get(tid, 0), "lineup_set": lset.get(tid, 0) >= 9,
                    "adjusted": tid in adjusted}
        board = []
        for m in conn.execute(
            "SELECT m.kind, m.home_team_id h, m.away_team_id a, th.name hn, ta.name an "
            "FROM matchups m JOIN teams th ON th.id=m.home_team_id "
            "JOIN teams ta ON ta.id=m.away_team_id WHERE m.season_id=? AND m.ff_week=?", (sid, wk)):
            board.append({"kind": m["kind"], "home": side(m["h"], m["hn"]),
                          "away": side(m["a"], m["an"])})

        tx = {"BLUE": [], "RED": []}
        for r in conn.execute(
            "SELECT tr.id id, tr.ff_week w, t.name team, c.code conf, tr.type, tr.position pos, "
            "tr.out_asset_kind ok, tr.out_asset_ref oref, tr.in_asset_kind ik, tr.in_asset_ref iref "
            "FROM transactions tr JOIN teams t ON t.id=tr.team_id "
            "JOIN conferences c ON c.id=t.conference_id WHERE tr.season_id=? AND tr.reversed=0 "
            "ORDER BY tr.ff_week DESC, tr.id DESC LIMIT 20", (sid,)):
            tx[r["conf"]].append({"id": r["id"], "week": r["w"], "team": r["team"], "type": r["type"],
                **_tx_parts(conn, sid, r["pos"], r["ok"], r["oref"], r["ik"], r["iref"])})

        fees = {t["team_id"]: _account(conn, sid, t["team_id"])
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
                               "setup_locked": repo.is_setup_locked(conn, sid),
                               "pin_setup_open": auth.pin_setup_open(conn, sid),
                               "seasons": [{"id": r["id"], "year": r["year"], "label": r["label"]}
                                           for r in all_seasons]},
                       standings=stand, scoreboard=board, fees=fees, pool=pool,
                       transactions=tx, lineups=lineups, byes=byes,
                       payout=st.final_payout(conn, sid))

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
        fees = _account(conn, sid, team_id)
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
        tw = conn.execute("SELECT computed_points, adjusted FROM team_week_scores "
                          "WHERE season_id=? AND team_id=? AND ff_week=?",
                          (sid, team_id, wk)).fetchone()
        return jsonify(name=row["name"], managers=row["manager_names"],
                       roster=roster, fees=fees, history=hist, payments=pays, opens=opens,
                       box=scoring.box_score(conn, sid, wk, team_id),
                       adjusted=bool(tw and tw["adjusted"]),
                       team_total=(tw["computed_points"] if tw else None))

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
        return jsonify(ok=True, transaction_id=tx, **_tx_fee_info(tx, team_id))

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
        return jsonify(ok=True, transaction_id=tx, **_tx_fee_info(tx, team_id))

    @app.post("/api/team/<int:team_id>/lineup")
    def lineup(team_id):
        if (bad := _guard(team_id)):
            return bad
        b = request.get_json(force=True)
        s = season(); sid, wk = s["id"], _week(s["current_ff_week"])
        is_comm = auth.is_commissioner(db(), _passcode())
        if wk != s["current_ff_week"] and not is_comm:
            return jsonify(error="only the commissioner can change another week's lineup"), 400
        try:
            from ..league.locks import locked_assets
            locked = set() if is_comm else locked_assets(db(), sid, wk)   # commissioner bypasses kickoff locks
            repo.set_lineup(db(), sid, team_id, wk, b["starters"], locked_refs=locked)
        except repo.RuleError as e:
            return jsonify(error=str(e)), 400
        # If this week is already scored, refresh the stored team total so the
        # scoreboard/standings match the new lineup (the box score already sums
        # the live lineup). Skipped for a not-yet-scored week.
        if db().execute("SELECT 1 FROM asset_week_scores WHERE season_id=? AND ff_week=? LIMIT 1",
                        (sid, wk)).fetchone():
            scoring.score_team_week(db(), sid, wk)
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

    # ---- manager PINs ----
    @app.post("/api/team/<int:team_id>/claim-pin")
    def claim_pin(team_id):
        b = request.get_json(force=True)
        try:
            auth.claim_team_pin(db(), season()["id"], team_id, b.get("pin", ""),
                                b.get("manager_names"))
        except auth.PinError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    @app.post("/api/team/<int:team_id>/change-pin")
    def change_pin(team_id):
        b = request.get_json(force=True)
        try:
            auth.change_team_pin(db(), team_id, b.get("current_pin", ""), b.get("new_pin", ""))
        except auth.PinError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    # ---- commissioner admin (commissioner passcode only) ----
    def _commish():
        return None if auth.is_commissioner(db(), _passcode()) else \
            (jsonify(error="commissioner passcode required"), 403)

    @app.post("/api/admin/team/<int:team_id>")
    def admin_set_team(team_id):
        if (bad := _commish()):
            return bad
        b = request.get_json(force=True)
        sid_row = db().execute("SELECT season_id FROM teams WHERE id=?", (team_id,)).fetchone()
        if not sid_row:
            return jsonify(error="unknown team"), 404
        # Name + team_number are season-constant once locked.
        if repo.is_setup_locked(db(), sid_row["season_id"]) and \
                any(c in b and b[c] not in (None, "") for c in ("name", "team_number")):
            return jsonify(error="season setup is locked — unlock it first to change "
                                 "team names or numbers"), 400
        num_changed = False
        for col in ("name", "team_number", "draft_slot", "manager_names"):
            if col in b and b[col] not in (None, ""):
                val = int(b[col]) if col in ("team_number", "draft_slot") else b[col]
                try:
                    db().execute(f"UPDATE teams SET {col}=? WHERE id=?", (val, team_id))
                except Exception as e:  # e.g. UNIQUE team_number / name collision
                    return jsonify(error=f"{col}: {e}"), 400
                if col == "team_number":
                    num_changed = True
        # The schedule is derived from team numbers — rebuild it if one changed.
        if num_changed:
            from ..league import standings as st
            sid = db().execute("SELECT season_id FROM teams WHERE id=?", (team_id,)).fetchone()["season_id"]
            if not db().execute("SELECT 1 FROM teams WHERE season_id=? AND team_number IS NULL",
                                (sid,)).fetchone():
                st.generate_matchups(db(), sid)
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/admin/season/lock")
    def admin_season_lock():
        if (bad := _commish()):
            return bad
        b = request.get_json(force=True)
        locked = bool(b.get("locked"))
        conn, sid = db(), season()["id"]
        if locked:
            missing = conn.execute(
                "SELECT COUNT(*) c FROM teams WHERE season_id=? AND team_number IS NULL",
                (sid,)).fetchone()["c"]
            if missing:
                return jsonify(error=f"{missing} team(s) still have no Team # — "
                                     f"fill those in before locking"), 400
        repo.set_setup_locked(conn, sid, locked)
        return jsonify(ok=True, locked=locked)

    @app.post("/api/admin/pin-setup")
    def admin_pin_setup():
        if (bad := _commish()):
            return bad
        is_open = bool(request.get_json(force=True).get("open"))
        auth.set_pin_setup_open(db(), season()["id"], is_open)
        return jsonify(ok=True, open=is_open)

    @app.post("/api/admin/season/delete")
    def admin_delete_season():
        if (bad := _commish()):
            return bad
        b = request.get_json(force=True)
        conn = db()
        sid = int(b.get("season_id") or 0)
        row = conn.execute("SELECT label FROM seasons WHERE id=?", (sid,)).fetchone()
        if not row:
            return jsonify(error="no such season"), 404
        # Typing the label is the guard against a mis-click wiping a season.
        if (b.get("confirm_label") or "").strip() != row["label"]:
            return jsonify(error=f'type the season label exactly ("{row["label"]}") to confirm'), 400
        from ..league import setup
        try:
            res = setup.delete_season(conn, sid)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, **res)

    @app.post("/api/admin/new-season")
    def admin_new_season():
        if (bad := _commish()):
            return bad
        b = request.get_json(force=True)
        year = int(b.get("year") or 2026)
        # The practice universe exists to rehearse scoring, so its FF week 1 is
        # pinned to NFL week 1 — otherwise nothing would score until NFL week 3
        # and there'd be nothing to watch. The real league keeps its wk3 start.
        offset = 1 if _is_dark() else 3
        from ..league import setup
        try:
            sid = setup.create_season(db(), year, b.get("label"), ff_start_nfl_week=offset)
        except (ValueError, RuntimeError) as e:
            return jsonify(error=str(e)), 400
        row = db().execute("SELECT label FROM seasons WHERE id=?", (sid,)).fetchone()
        teams = db().execute("SELECT COUNT(*) c FROM teams WHERE season_id=?", (sid,)).fetchone()["c"]
        return jsonify(ok=True, season_id=sid, label=row["label"], teams=teams,
                       ff_start_nfl_week=offset)

    @app.post("/api/admin/team/<int:team_id>/passcode")
    def admin_set_passcode(team_id):
        if (bad := _commish()):
            return bad
        new = request.get_json(force=True).get("new_passcode")
        if not new:
            return jsonify(error="new_passcode required"), 400
        auth.set_team_passcode(db(), team_id, new)
        return jsonify(ok=True)

    @app.post("/api/admin/team/<int:team_id>/score")
    def admin_set_score(team_id):
        if (bad := _commish()):
            return bad
        b = request.get_json(force=True)
        wk, pts = int(b["week"]), float(b["points"])
        db().execute(
            "INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points,adjusted,computed_at) "
            "VALUES (?,?,?,?,1, datetime('now')) ON CONFLICT(season_id,team_id,ff_week) DO UPDATE SET "
            "computed_points=excluded.computed_points, adjusted=1, computed_at=datetime('now')",
            (season()["id"], team_id, wk, pts))
        db().commit()
        return jsonify(ok=True)

    @app.post("/api/admin/run-current")
    def admin_run_current():
        if (bad := _commish()):
            return bad
        from ..league.runner import run_current
        r = run_current(db(), season()["id"])
        return jsonify(ok=True, scored_weeks=r["scored"], live_weeks=r["live"])

    @app.post("/api/admin/reverse/<int:tx_id>")
    def admin_reverse(tx_id):
        if (bad := _commish()):
            return bad
        repo.reverse_transaction(db(), tx_id)
        return jsonify(ok=True)

    @app.post("/api/admin/convert/<int:tx_id>")
    def admin_convert(tx_id):
        if (bad := _commish()):
            return bad
        try:
            new_id = repo.convert_transaction(db(), season()["id"], tx_id)
        except repo.RuleError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, transaction_id=new_id)

    # ---- practice universe -------------------------------------------------
    # Mirror every route under /dark. Same view functions, same templates; only
    # db() differs, keyed off the request path. Registered last so it picks up
    # everything defined above.
    for rule in list(app.url_map.iter_rules()):
        if rule.endpoint == "static" or rule.rule.startswith(DARK_PREFIX):
            continue
        app.add_url_rule(DARK_PREFIX + rule.rule,
                         endpoint="dark__" + rule.endpoint,
                         view_func=app.view_functions[rule.endpoint],
                         methods=sorted(rule.methods - {"HEAD", "OPTIONS"}))

    # Convenience aliases so the practice board is reachable by the name Scott
    # uses for it. Redirects (not handlers) so the /dark prefix still selects
    # the practice DB.
    @app.get("/otblitzdark")
    def otblitz_dark_alias():
        return redirect(DARK_PREFIX + "/otblitz")

    @app.get("/darkotblitz")
    def otblitz_dark_alias2():
        return redirect(DARK_PREFIX + "/otblitz")

    return app
