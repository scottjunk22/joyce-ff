"""
Weekly scoring pipeline.

Two layers, split so the pure DB logic is testable without network:
  * ingest_asset_scores_from_nflverse() — pulls real stats for the NFL week,
    scores every player/unit with the validated engine, stores per-asset lines
    (with breakdown) in asset_week_scores, and locks each game the first time
    its stats are complete.
  * score_team_week() / box_score() — pure DB: sum a team's started assets
    from its lineup, using the stored asset scores. No network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def nfl_week_for(conn, season_id: int, ff_week: int) -> int:
    row = conn.execute("SELECT ff_start_nfl_week FROM seasons WHERE id=?", (season_id,)).fetchone()
    start = row["ff_start_nfl_week"] if row else 3
    return ff_week + (start - 1)


def _upsert_asset(conn, season_id, ff_week, kind, ref, unit, breakdown):
    # unit_type is part of the key (one NFL team = 4 units); players use ''.
    conn.execute(
        "INSERT INTO asset_week_scores(season_id,ff_week,asset_kind,asset_ref,unit_type,"
        "points,breakdown_json,computed_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(season_id,ff_week,asset_kind,asset_ref,unit_type) DO UPDATE SET "
        "points=excluded.points, breakdown_json=excluded.breakdown_json, "
        "computed_at=excluded.computed_at",
        (season_id, ff_week, kind, ref, unit or "", breakdown.total,
         json.dumps(breakdown.items), _now()))


def ingest_asset_scores_from_nflverse(conn, season_id: int, ff_week: int) -> int:
    """Score every player + team unit for the NFL week behind this FF week and
    store them. Returns the number of asset lines written. Needs nflverse data.

    Per-game lock: the first run after a game's play-by-play reaches END GAME,
    with its final score posted in the schedule, writes that game's lines one
    last time and locks the game. From then on every line from it is left
    alone, so a stat correction published on Monday can't move a Sunday result.
    The commissioner scores from the box score as it stood; his score override
    is the way to correct one.
    """
    from ..data_sources import nflverse as nv
    from ..scoring import engine as E
    from ..scoring.models import (CoachUnitGame, DefenseUnitGame, KickerUnitGame,
                                  PlayerGame, QBUnitGame)
    from .progress import locked_teams

    year = conn.execute("SELECT year FROM seasons WHERE id=?", (season_id,)).fetchone()["year"]
    wk = nfl_week_for(conn, season_id, ff_week)
    pbp = nv.load_pbp(year)
    pbp = pbp[pbp["season_type"].isin(["REG", "POST"])]
    games = nv.load_games()
    frozen = locked_teams(conn, season_id, ff_week)     # games locked on an earlier run
    finished = nv.finished_game_ids(pbp)
    game_of = {}
    for g in games[(games["season"] == year) & (games["week"] == wk)].to_dict("records"):
        game_of[g["home_team"]] = game_of[g["away_team"]] = g["game_id"]
    espn_locked = {r["game_id"] for r in conn.execute(
        "SELECT game_id FROM nfl_game_locks WHERE season_id=? AND ff_week=? AND source='espn'",
        (season_id, ff_week))}
    n = 0

    def keep(team, kind, ref, unit, b):
        """Write a line — or, for a game already locked from ESPN that nflverse
        now has complete, compare against what stands and note any difference."""
        nonlocal n
        if team not in frozen:
            _upsert_asset(conn, season_id, ff_week, kind, ref, unit, b)
            n += 1
        elif game_of.get(team) in espn_locked and game_of.get(team) in finished:
            _check_locked(conn, season_id, ff_week, kind, ref, unit, b.total, "nflverse")

    pw = nv.player_week_stats(pbp)
    for _, r in pw[pw["week"] == wk].iterrows():
        keep(r["team"], "PLAYER", r["player_id"], None, E.score_player_game(PlayerGame(
            player=r["name"], team=r["team"], rushing_yards=r.rushing_yards,
            rushing_tds=r.rushing_tds, receiving_yards=r.receiving_yards,
            receptions=r.receptions, receiving_tds=r.receiving_tds, return_tds=r.return_tds)))

    qb = nv.qb_unit_week_stats(pbp)
    for _, r in qb[qb["week"] == wk].iterrows():
        keep(r.team, "TEAM_UNIT", r.team, "QB", E.score_qb_unit_game(QBUnitGame(
            team=r.team, passing_yards=r.passing_yards, passing_tds=r.passing_tds)))

    kk = nv.kicker_unit_week_stats(pbp)
    for _, r in kk[kk["week"] == wk].iterrows():
        keep(r.team, "TEAM_UNIT", r.team, "K", E.score_kicker_unit_game(KickerUnitGame(
            team=r.team, field_goal_distances=tuple(r.fg_distances),
            extra_points_made=int(r.extra_points_made))))

    dd = nv.defense_unit_week_stats(pbp, games, year)
    for _, r in dd[dd["week"] == wk].iterrows():
        keep(r.team, "TEAM_UNIT", r.team, "DEF/ST", E.score_defense_unit_game(DefenseUnitGame(
            team=r.team, points_allowed=int(r.points_allowed), yards_allowed=int(r.yards_allowed),
            sacks=int(r.sacks), interceptions=int(r.interceptions),
            fumble_recoveries=int(r.fumble_recoveries), safeties=int(r.safeties),
            defensive_tds=int(r.defensive_tds), special_teams_tds=int(r.special_teams_tds))))

    cc = nv.coach_unit_week_stats(games, year)
    for _, r in cc[cc["week"] == wk].iterrows():
        keep(r.team, "TEAM_UNIT", r.team, "C", E.score_coach_unit_game(CoachUnitGame(
            team=r.team, won=bool(r.won), tied=bool(r.tied))))

    _lock_finished_games(conn, season_id, ff_week, finished, games, year, wk)
    conn.commit()
    return n


def _lock_finished_games(conn, season_id, ff_week, finished, games, year, wk) -> int:
    """Lock every game this week whose play-by-play has reached END GAME and
    whose final score is posted — the coach's win and the defense's points
    allowed both come from that score, so it has to be in too. The lines just
    written for these games are the ones that stand. Returns how many locked."""
    import pandas as pd

    week = games[(games["season"] == year) & (games["week"] == wk)]
    n = 0
    for r in week.to_dict("records"):
        if (r["game_id"] in finished and pd.notna(r["home_score"])
                and pd.notna(r["away_score"])):
            n += conn.execute(
                "INSERT OR IGNORE INTO nfl_game_locks(season_id,ff_week,game_id,home_team,"
                "away_team,locked_at,source) VALUES (?,?,?,?,?,?,'nflverse')",
                (season_id, ff_week, r["game_id"], r["home_team"], r["away_team"],
                 _now())).rowcount
    return n


def _check_locked(conn, season_id, ff_week, kind, ref, unit, points, source) -> None:
    """Note it when another source's complete line for a LOCKED asset scores
    differently. Only for assets someone actually started that week — a
    backup nobody owns isn't worth the commissioner's attention."""
    started = conn.execute(
        "SELECT 1 FROM weekly_lineups WHERE season_id=? AND ff_week=? AND asset_kind=? "
        "AND asset_ref=? AND (asset_kind='PLAYER' OR roster_slot=?) LIMIT 1",
        (season_id, ff_week, kind, ref, unit)).fetchone()
    if not started:
        return
    row = conn.execute(
        "SELECT points FROM asset_week_scores WHERE season_id=? AND ff_week=? AND asset_kind=? "
        "AND asset_ref=? AND unit_type=?", (season_id, ff_week, kind, ref, unit or "")).fetchone()
    locked = float(row["points"]) if row else 0.0
    if locked != float(points):
        conn.execute(
            "INSERT OR IGNORE INTO stat_checks(season_id,ff_week,asset_kind,asset_ref,unit_type,"
            "locked_points,other_points,source,noted_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (season_id, ff_week, kind, ref, unit or "", locked, float(points), source, _now()))


# --- ESPN: the fast source --------------------------------------------------

SOURCES = ("espn", "nflverse")
# How long ESPN must show a game Final before it's locked, so the last
# box-score touch-ups after the final whistle land first.
ESPN_SETTLE_MINUTES = 10
_PLAYER_FIELDS = ("rushing_yards", "rushing_tds", "receptions", "receiving_yards",
                  "receiving_tds", "return_tds")


def _espn_to_gsis(year: int) -> dict[str, str]:
    """ESPN athlete id -> gsis id (our player id), from nflverse's roster."""
    import pandas as pd

    from ..data_sources import nflverse as nv

    r = nv.load_roster(year)
    if "espn_id" not in r.columns:
        return {}
    out = {}
    for e, g in zip(r["espn_id"], r["gsis_id"]):
        if pd.notna(e) and pd.notna(g):
            try:
                out[str(int(float(e)))] = g
            except ValueError:
                pass
    return out


def _norm_name(name) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _roster_by_name(year: int) -> dict[tuple[str, str], str]:
    """(squashed full name, team) -> gsis id, from nflverse's roster, where
    that pair is unique. nflverse is missing ESPN ids for some players — a
    rookie RB (Mike Washington Jr., LV) and a lineman in 2026 Week 1 — and
    without this their games couldn't lock from ESPN."""
    import pandas as pd

    from ..data_sources import nflverse as nv

    r = nv.load_roster(year)
    seen: dict[tuple[str, str], list] = {}
    for n, t, g in zip(r["full_name"], r["team"], r["gsis_id"]):
        if pd.notna(n) and pd.notna(g):
            seen.setdefault((_norm_name(n), t), []).append(g)
    return {k: v[0] for k, v in seen.items() if len(v) == 1}


def _gsis_by_name(conn, season_id, name, team):
    rows = conn.execute("SELECT gsis_id FROM nfl_players WHERE season_id=? AND nfl_team_abbr=? "
                        "AND lower(name)=lower(?)", (season_id, team, name)).fetchall()
    return rows[0]["gsis_id"] if len(rows) == 1 else None


def _final_long_enough(conn, season_id, game_id, now) -> bool:
    """True once ESPN has shown this game Final for ESPN_SETTLE_MINUTES."""
    import datetime as _dt

    key = f"espn_final_seen:{season_id}:{game_id}"
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, now.isoformat()))
        return False
    return now - _dt.datetime.fromisoformat(row["value"]) >= _dt.timedelta(minutes=ESPN_SETTLE_MINUTES)


def ingest_espn_week(conn, season_id: int, ff_week: int, now=None) -> int:
    """Score every game in this week that ESPN shows started, and lock a game
    ESPN has shown Final for a few minutes — but only when every part of it is
    understood: each scoring play classified and every player with stats matched
    to ours. Otherwise the game is left unlocked for nflverse to settle, rather
    than locking a guess. Returns the number of asset lines written."""
    import datetime as _dt

    from ..data_sources import espn
    from ..data_sources import nflverse as nv
    from ..scoring import engine as E
    from ..scoring.models import (CoachUnitGame, DefenseUnitGame, KickerUnitGame,
                                  PlayerGame, QBUnitGame)
    from .progress import ET, locked_teams

    now = now or _dt.datetime.now(ET)
    year = conn.execute("SELECT year FROM seasons WHERE id=?", (season_id,)).fetchone()["year"]
    wk = nfl_week_for(conn, season_id, ff_week)
    events = [e for e in espn.week_events(year, wk) if e.state in ("in", "post")]
    if not events:
        return 0
    frozen = locked_teams(conn, season_id, ff_week)
    games = nv.load_games()
    ids = {frozenset((g["home_team"], g["away_team"])): g["game_id"]
           for g in games[(games["season"] == year) & (games["week"] == wk)].to_dict("records")}
    to_gsis = _espn_to_gsis(year)
    by_name = None                              # loaded only if an ESPN id isn't on file
    n = 0
    for ev in events:
        if ev.home in frozen and ev.away in frozen:
            continue
        g = espn.game_lines(ev.event_id)
        unmatched = []
        for aid, p in g.players.items():
            if p["team"] in frozen:
                continue
            gsis = to_gsis.get(aid) or _gsis_by_name(conn, season_id, p["name"], p["team"])
            if gsis is None:
                if by_name is None:
                    by_name = _roster_by_name(year)
                gsis = by_name.get((_norm_name(p["name"]), p["team"]))
            if gsis is None:
                if any(p.get(f) for f in _PLAYER_FIELDS):
                    unmatched.append(p["name"])
                continue
            b = E.score_player_game(PlayerGame(player=p["name"], team=p["team"],
                                               **{f: int(p.get(f, 0)) for f in _PLAYER_FIELDS}))
            _upsert_asset(conn, season_id, ff_week, "PLAYER", gsis, None, b)
            n += 1
        for ab, u in g.units.items():
            if ab in frozen:
                continue
            i = lambda k: int(round(u[k]))
            _upsert_asset(conn, season_id, ff_week, "TEAM_UNIT", ab, "QB", E.score_qb_unit_game(
                QBUnitGame(team=ab, passing_yards=i("passing_yards"), passing_tds=i("passing_tds"))))
            _upsert_asset(conn, season_id, ff_week, "TEAM_UNIT", ab, "K", E.score_kicker_unit_game(
                KickerUnitGame(team=ab, field_goal_distances=tuple(u["fg_distances"]),
                               extra_points_made=i("extra_points_made"))))
            _upsert_asset(conn, season_id, ff_week, "TEAM_UNIT", ab, "DEF/ST", E.score_defense_unit_game(
                DefenseUnitGame(team=ab, points_allowed=i("points_allowed"),
                                yards_allowed=i("yards_allowed"), sacks=i("sacks"),
                                interceptions=i("interceptions"),
                                fumble_recoveries=i("fumble_recoveries"), safeties=i("safeties"),
                                defensive_tds=i("defensive_tds"),
                                special_teams_tds=i("special_teams_tds"))))
            _upsert_asset(conn, season_id, ff_week, "TEAM_UNIT", ab, "C", E.score_coach_unit_game(
                CoachUnitGame(team=ab, won=u["won"], tied=u["tied"])))
            n += 4
        if not ev.completed:
            continue
        if g.unknown_scoring or unmatched:
            print(f"  ESPN: not locking {ev.away} @ {ev.home} — "
                  + "; ".join([f"unrecognised scoring play {k!r}" for k in g.unknown_scoring]
                              + [f"no player match for {m}" for m in unmatched])
                  + " (nflverse will settle it)")
            continue
        if _final_long_enough(conn, season_id, ids.get(frozenset((ev.home, ev.away)), ev.game_id), now):
            conn.execute(
                "INSERT OR IGNORE INTO nfl_game_locks(season_id,ff_week,game_id,home_team,away_team,"
                "locked_at,source) VALUES (?,?,?,?,?,?,'espn')",
                (season_id, ff_week, ids.get(frozenset((ev.home, ev.away)), ev.game_id),
                 ev.home, ev.away, _now()))
    conn.commit()
    return n


def ingest_week(conn, season_id: int, ff_week: int, sources=SOURCES, now=None) -> dict:
    """Pull the week's stats from each source in turn. ESPN is the fast path;
    nflverse is the backup (it can lock a game ESPN couldn't settle) and the
    later cross-check of what ESPN locked. Whichever source locks a game first,
    its box score stands. One source failing is fine; if every source fails,
    the last error is raised so the site can say why."""
    done, errors = {}, []
    for src in sources:
        try:
            done[src] = (ingest_espn_week(conn, season_id, ff_week, now=now) if src == "espn"
                         else ingest_asset_scores_from_nflverse(conn, season_id, ff_week))
        except Exception as e:      # one source down mustn't stop the other
            conn.rollback()
            errors.append(e)
            print(f"  {src} unavailable for FF week {ff_week}: {type(e).__name__}: {e}")
    if not done:
        raise errors[-1]
    return done


def score_team_week(conn, season_id: int, ff_week: int) -> None:
    """Sum each team's started assets (from weekly_lineups) using stored asset
    scores, and upsert team_week_scores.computed_points."""
    teams = [r["team_id"] for r in conn.execute(
        "SELECT DISTINCT team_id FROM weekly_lineups WHERE season_id=? AND ff_week=?",
        (season_id, ff_week))]
    for team_id in teams:
        total = conn.execute(
            "SELECT COALESCE(SUM(a.points),0) s FROM weekly_lineups l "
            "JOIN asset_week_scores a ON a.season_id=l.season_id AND a.ff_week=l.ff_week "
            "AND a.asset_kind=l.asset_kind AND a.asset_ref=l.asset_ref "
            "AND (l.asset_kind='PLAYER' OR a.unit_type=l.roster_slot) "
            "WHERE l.season_id=? AND l.ff_week=? AND l.team_id=?",
            (season_id, ff_week, team_id)).fetchone()["s"]
        conn.execute(
            "INSERT INTO team_week_scores(season_id,team_id,ff_week,computed_points,computed_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(season_id,team_id,ff_week) DO UPDATE SET "
            "computed_points=excluded.computed_points, computed_at=excluded.computed_at "
            "WHERE team_week_scores.adjusted=0",   # never clobber a commissioner override
            (season_id, team_id, ff_week, total, _now()))
    conn.commit()


def box_score(conn, season_id: int, ff_week: int, team_id: int) -> list[dict]:
    """The team's starters with their points + breakdown, for the box-score UI."""
    rows = conn.execute(
        "SELECT l.roster_slot, l.asset_kind, l.asset_ref, l.unit_type, l.is_rental, "
        "COALESCE(a.points,0) points, a.breakdown_json, p.name player_name "
        "FROM weekly_lineups l "
        "LEFT JOIN asset_week_scores a ON a.season_id=l.season_id AND a.ff_week=l.ff_week "
        "AND a.asset_kind=l.asset_kind AND a.asset_ref=l.asset_ref "
        "AND (l.asset_kind='PLAYER' OR a.unit_type=l.roster_slot) "
        "LEFT JOIN nfl_players p ON p.season_id=l.season_id AND p.gsis_id=l.asset_ref "
        "WHERE l.season_id=? AND l.ff_week=? AND l.team_id=? "
        "ORDER BY CASE l.roster_slot WHEN 'C' THEN 0 WHEN 'K' THEN 1 WHEN 'DEF/ST' THEN 2 "
        "WHEN 'QB' THEN 3 WHEN 'RB' THEN 4 WHEN 'R' THEN 5 ELSE 6 END, l.id",
        (season_id, ff_week, team_id))
    out = []
    for r in rows:
        d = dict(r)
        d["display"] = d.pop("player_name") or f"{d['asset_ref']} {d['unit_type'] or ''}".strip()
        d["breakdown"] = json.loads(d.pop("breakdown_json") or "[]")
        out.append(d)
    return out
