"""
Season setup: load the NFL universe (players + team units with bye weeks) from
nflverse, assign team numbers / draft slots, and materialize the schedule.
"""

from __future__ import annotations


def _nfl_byes(g) -> dict[str, int | None]:
    """NFL bye week per team = the regular-season week (1-18) it doesn't play."""
    reg = g[g["game_type"] == "REG"]
    teams = sorted(set(reg["home_team"]) | set(reg["away_team"]))
    byes = {}
    for abbr in teams:
        played = set(reg[(reg["home_team"] == abbr) | (reg["away_team"] == abbr)]["week"])
        missing = [w for w in range(1, 19) if w not in played]
        byes[abbr] = missing[0] if missing else None
    return byes


def load_nfl_universe(conn, season_id: int, year: int) -> tuple[int, int]:
    """Populate nfl_teams (with FF bye week) and nfl_players (RB/WR/TE)."""
    from ..data_sources import nflverse as nv

    ff_start = conn.execute("SELECT ff_start_nfl_week FROM seasons WHERE id=?",
                            (season_id,)).fetchone()["ff_start_nfl_week"]
    games = nv.load_games()
    g = games[games["season"] == year]
    byes = _nfl_byes(g)
    for abbr, nfl_bye in byes.items():
        bye_ff = (nfl_bye - (ff_start - 1)) if nfl_bye else None
        conn.execute("INSERT OR IGNORE INTO nfl_teams(season_id,abbr,name,bye_ff_week) "
                     "VALUES (?,?,?,?)", (season_id, abbr, abbr, bye_ff))

    roster = nv.load_roster(year)
    ros = roster[roster["position"].isin(["RB", "WR", "TE"])]
    n = 0
    for _, r in ros.iterrows():
        if not r.get("gsis_id"):
            continue
        conn.execute("INSERT OR IGNORE INTO nfl_players(season_id,gsis_id,name,position,"
                     "nfl_team_abbr,status) VALUES (?,?,?,?,?,?)",
                     (season_id, r["gsis_id"], r["full_name"], r["position"],
                      r.get("team"), r.get("status")))
        n += 1
    conn.commit()
    return len(byes), n


PLAYER_POSITIONS = ("RB", "WR", "TE")


def refresh_nfl_players(conn, season_id: int, year: int, roster=None) -> dict:
    """Bring the player pool up to date with nflverse's season roster
    (commissioner, 2026-09-16). The pool was only ever copied when the season
    was created, so a player traded to another NFL team kept his old team — and
    with it the wrong bye, game time and kickoff lock — and a call-up or signing
    couldn't be picked up at all.

      * existing players: name, position, NFL team and status follow the roster;
      * new RBs / WRs / TEs are added;
      * nobody is removed — a player a team owns never drops off its roster.
    Returns {"updated": n, "added": n}."""
    import pandas as pd

    from ..data_sources import nflverse as nv

    roster = nv.load_roster(year) if roster is None else roster
    have = {r["gsis_id"]: r for r in conn.execute(
        "SELECT gsis_id, name, position, nfl_team_abbr, status FROM nfl_players WHERE season_id=?",
        (season_id,))}
    val = lambda v: None if v is None or (isinstance(v, float) and pd.isna(v)) else v
    updated = added = 0
    for r in roster.to_dict("records"):
        gid = val(r.get("gsis_id"))
        if not gid:
            continue
        name, pos, team, status = (val(r.get("full_name")), val(r.get("position")),
                                   val(r.get("team")), val(r.get("status")))
        old = have.get(gid)
        if old is None:
            if pos in PLAYER_POSITIONS and name and team:
                conn.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr,status) "
                             "VALUES (?,?,?,?,?,?)", (season_id, gid, name, pos, team, status))
                added += 1
            continue
        new = (name or old["name"], pos or old["position"], team or old["nfl_team_abbr"], status)
        if new != (old["name"], old["position"], old["nfl_team_abbr"], old["status"]):
            conn.execute("UPDATE nfl_players SET name=?, position=?, nfl_team_abbr=?, status=? "
                         "WHERE season_id=? AND gsis_id=?", (*new, season_id, gid))
            updated += 1
    conn.commit()
    return {"updated": updated, "added": added}


def refresh_nfl_players_daily(conn, season_id: int, year: int, now) -> dict | None:
    """refresh_nfl_players at most once per Central calendar day. None if it
    already ran today."""
    from .progress import CT

    key, today = f"players_refreshed:{season_id}", now.astimezone(CT).date().isoformat()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and row["value"][:10] == today:
        return None
    res = refresh_nfl_players(conn, season_id, year)
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                 (key, now.astimezone(CT).isoformat(timespec="minutes")))
    conn.commit()
    return res


def assign_numbers_and_slots(conn, season_id: int) -> None:
    """Give each team a team_number (schedule) and draft_slot (card), 1-11 per
    conference. Deterministic — real draws replace these on draft day."""
    for conf in ("BLUE", "RED"):
        rows = conn.execute(
            "SELECT t.id FROM teams t JOIN conferences c ON c.id=t.conference_id "
            "WHERE t.season_id=? AND c.code=? ORDER BY t.id", (season_id, conf)).fetchall()
        for i, r in enumerate(rows, 1):
            conn.execute("UPDATE teams SET team_number=?, draft_slot=? WHERE id=?",
                         (i, i, r["id"]))
    conn.commit()


def prepare_season(conn, season_id: int, year: int) -> None:
    from . import standings as st
    load_nfl_universe(conn, season_id, year)
    assign_numbers_and_slots(conn, season_id)
    st.generate_matchups(conn, season_id)


# Every table that hangs off a season, children first so foreign keys hold.
SEASON_TABLES = ("roster_entries", "weekly_lineups", "transactions", "payments",
                 "asset_week_scores", "nfl_game_locks", "nfl_week_games", "stat_checks", "espn_snapshots", "tiebreak_decisions",
                 "team_week_scores", "matchups",
                 "teams", "nfl_teams", "nfl_players")


def delete_season(conn, season_id: int) -> dict:
    """Delete a season and everything under it. Used to redo a botched setup
    and to wipe a practice run. Irreversible — the caller is responsible for
    confirming intent."""
    row = conn.execute("SELECT label FROM seasons WHERE id=?", (season_id,)).fetchone()
    if not row:
        raise ValueError("no such season")
    counts = {}
    for t in SEASON_TABLES:
        cur = conn.execute(f"DELETE FROM {t} WHERE season_id=?", (season_id,))
        if cur.rowcount:
            counts[t] = cur.rowcount
    # Season-scoped settings: draft clock, setup lock, finalized and in-progress
    # weeks, kickoff times, PIN window, the last scoring note.
    conn.execute("DELETE FROM settings WHERE key LIKE ? OR key LIKE ? OR key LIKE ? "
                 "OR key LIKE ? OR key LIKE ? OR key LIKE ? OR key LIKE ? OR key=? OR key=? OR key=?",
                 (f"draft_cursor:{season_id}:%", f"week_final:{season_id}:%",
                  f"week_live:{season_id}:%", f"kickoffs:{season_id}:%",
                  f"espn_final_seen:{season_id}:%", f"pin_reset:{season_id}:%", f"score_check_dismissed:{season_id}:%",
                  f"setup_locked:{season_id}", f"players_refreshed:{season_id}",
                  f"scoring_note:{season_id}"))
    conn.execute("DELETE FROM seasons WHERE id=?", (season_id,))
    conn.commit()
    return {"label": row["label"], "deleted": counts}


def create_season(conn, year: int, label: str | None = None,
                  current_ff_week: int = 1, status: str = "drafting",
                  ff_start_nfl_week: int = 3) -> int:
    """Stand up a brand-new season ready for the draft: 22 generic-named teams
    (Blue 1..11, Red 1..11), the NFL universe for `year`, deterministic
    team_number/draft_slot (1-11 per conference), and the full matchup schedule.

    The public site shows the newest `year`, so creating this flips the site to
    the new season at Week 1 with the schedule on the scoreboard and empty
    rosters. Fails loudly (before creating anything) if the NFL data for `year`
    isn't cached yet — a missing source is a visible error, never a guess."""
    if conn.execute("SELECT 1 FROM seasons WHERE year=?", (year,)).fetchone():
        raise ValueError(f"a {year} season already exists")
    label = label or f"{year}-{str(year + 1)[2:]}"

    # Verify the NFL universe is available BEFORE mutating anything. These
    # calls auto-download from nflverse and cache; a missing source is a visible
    # error, never a guess.
    from ..data_sources import nflverse as nv
    try:
        games = nv.load_games()
        roster = nv.load_roster(year)
    except Exception as e:  # network / 404 for an unpublished season
        raise RuntimeError(f"couldn't fetch NFL {year} data from nflverse: {e}")
    if games.query("season == @year").empty:
        raise RuntimeError(f"nflverse has no {year} schedule yet "
                           f"(the {year} NFL season may not be published)")
    if len(roster) == 0:
        raise RuntimeError(f"nflverse has no {year} rosters yet")

    for code, cname in (("BLUE", "Blue Conference"), ("RED", "Red Conference")):
        conn.execute("INSERT OR IGNORE INTO conferences(code, name) VALUES (?, ?)", (code, cname))
    # ff_start_nfl_week is set BEFORE prepare_season, which derives every team's
    # bye FF week from it.
    conn.execute("INSERT INTO seasons(year, label, current_ff_week, status, ff_start_nfl_week) "
                 "VALUES (?,?,?,?,?)",
                 (year, label, current_ff_week, status, ff_start_nfl_week))
    sid = conn.execute("SELECT id FROM seasons WHERE year=?", (year,)).fetchone()["id"]
    conf_ids = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM conferences")}
    for code in ("BLUE", "RED"):
        for i in range(1, 12):
            conn.execute("INSERT INTO teams(season_id, name, conference_id) VALUES (?,?,?)",
                         (sid, f"{code.title()} {i}", conf_ids[code]))
    conn.commit()
    prepare_season(conn, sid, year)
    conn.commit()
    return sid
