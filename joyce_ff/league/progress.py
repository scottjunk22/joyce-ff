"""
Where a fantasy week stands, worked out from the database alone — no network,
so the site can ask on every page load.

Three facts drive it:
  * which NFL games are LOCKED — their stats are complete and frozen
    (nfl_game_locks, written by scoring.py the first time a game's play-by-play
    reaches END GAME with its final score posted);
  * which NFL teams are on bye this week (nfl_teams.bye_ff_week);
  * when the week's first and last games kick off (stored by the hourly
    runner, which is the part that reads the schedule).

From those, each fantasy team this week is:
  * done  — every starter's game is locked and nothing can still change the
            total. A starter on bye never locks (he has no game), and he could
            still be swapped out or covered by an Open until the week's last
            kickoff, so a team carrying one isn't done until then.
  * floor — the points already locked in. No slot in this league scores
            negative, so a team can never finish below its floor. That's what
            makes calling a matchup, or an elimination, before Monday night safe.

A week that has been finalized — or was scored before any of this existed and
isn't flagged in progress — is settled: every team in it is done.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# --- week-level flags (settings) --------------------------------------------

def _final_key(season_id: int, ff_week: int) -> str:
    return f"week_final:{season_id}:{ff_week}"


def is_finalized(conn, season_id: int, ff_week: int) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (_final_key(season_id, ff_week),)).fetchone()
    return bool(row) and row["value"] == "1"


def mark_finalized(conn, season_id: int, ff_week: int) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,'1')",
                 (_final_key(season_id, ff_week),))


# A week the hourly runner is scoring while its games are still going is
# flagged IN PROGRESS until its final run. Its running totals aren't results.
# The flag marks in-progress weeks rather than finished ones on purpose: weeks
# scored before it existed, or by the run-week command, carry no flag and keep
# counting exactly as they always have.

def _live_key(season_id: int, ff_week: int) -> str:
    return f"week_live:{season_id}:{ff_week}"


def mark_live(conn, season_id: int, ff_week: int) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,'1')",
                 (_live_key(season_id, ff_week),))


def clear_live(conn, season_id: int, ff_week: int) -> None:
    conn.execute("DELETE FROM settings WHERE key=?", (_live_key(season_id, ff_week),))


def live_weeks(conn, season_id: int) -> set[int]:
    prefix = f"week_live:{season_id}:"
    return {int(r["key"][len(prefix):]) for r in conn.execute(
        "SELECT key FROM settings WHERE key LIKE ?", (prefix + "%",))}


def _kick_key(season_id: int, ff_week: int) -> str:
    return f"kickoffs:{season_id}:{ff_week}"


def store_kickoffs(conn, season_id: int, ff_week: int,
                   first: _dt.datetime, last: _dt.datetime) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                 (_kick_key(season_id, ff_week), f"{first.isoformat()}|{last.isoformat()}"))


def kickoffs(conn, season_id: int, ff_week: int):
    """(first, last) kickoff of the week, or (None, None) if the runner hasn't
    recorded them yet."""
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (_kick_key(season_id, ff_week),)).fetchone()
    if not row:
        return None, None
    first, _, last = row["value"].partition("|")
    return _dt.datetime.fromisoformat(first), _dt.datetime.fromisoformat(last)


# --- which week the site is on --------------------------------------------------
# Two different clocks (commissioner, 2026-09-15):
#   * LINEUP WEEK — the week managers act on: set a lineup, trade, Open; the
#     commissioner's "lineups in" count. Moves to the next week at 6am Central on
#     the Tuesday after the week's last game, so there's time before Thursday.
#   * SCOREBOARD WEEK — the week the site opens to. Stays on the week just played
#     (its results, and the "no lineup" reminders once the next week is up) until
#     6am Central on the day the next week's first game kicks off — Thursday.
# Both come from the stored schedule kickoffs. A season whose last game is long
# over, or whose kickoffs aren't known, keeps the plain stored current week.

CT = ZoneInfo("America/Chicago")
SEASON_OVER_AFTER = _dt.timedelta(days=30)


def lineup_opens(last_kickoff: _dt.datetime) -> _dt.datetime:
    """6am Central on the first Tuesday after a week's last kickoff (the day
    after, in the rare week that ends on a Tuesday)."""
    lk = last_kickoff.astimezone(CT)
    day = lk.date() + _dt.timedelta(days=1)
    while day.weekday() != 1:                          # Tuesday
        day += _dt.timedelta(days=1)
    at = _dt.datetime.combine(day, _dt.time(6), CT)
    if at - lk > _dt.timedelta(days=3):
        at = _dt.datetime.combine(lk.date() + _dt.timedelta(days=1), _dt.time(6), CT)
    return at


def scoreboard_switch(first_kickoff: _dt.datetime) -> _dt.datetime:
    """6am Central on the day of a week's first kickoff."""
    return _dt.datetime.combine(first_kickoff.astimezone(CT).date(), _dt.time(6), CT)


def _week_clock(conn, season_id: int, now: _dt.datetime):
    """(lineup week, scoreboard week) from the schedule, or None when it can't
    be known (kickoffs not stored) or the season is over."""
    weeks = [r["ff_week"] for r in conn.execute(
        "SELECT DISTINCT ff_week FROM matchups WHERE season_id=? AND kind!='NO_PLAY' "
        "ORDER BY ff_week", (season_id,))]
    kicks = {w: kickoffs(conn, season_id, w) for w in weeks}
    lasts = [k[1] for k in kicks.values() if k[1] is not None]
    if not weeks or not lasts or now > max(lasts) + SEASON_OVER_AFTER:
        return None
    lineup = board = weeks[0]
    for prev, week in zip(weeks, weeks[1:]):
        last = kicks[prev][1]
        if last is None or now < lineup_opens(last):
            break
        lineup = week
        first = kicks[week][0]
        if first is not None and now >= max(scoreboard_switch(first), lineup_opens(last)):
            board = week
    return lineup, board


def _stored_week(conn, season_id: int) -> int:
    r = conn.execute("SELECT current_ff_week FROM seasons WHERE id=?", (season_id,)).fetchone()
    return max(1, r["current_ff_week"] if r else 1)


def lineup_week(conn, season_id: int, now: _dt.datetime | None = None) -> int:
    clock = _week_clock(conn, season_id, now or _dt.datetime.now(ET))
    stored = _stored_week(conn, season_id)
    return max(clock[0], stored) if clock else stored


def scoreboard_week(conn, season_id: int, now: _dt.datetime | None = None) -> int:
    clock = _week_clock(conn, season_id, now or _dt.datetime.now(ET))
    stored = _stored_week(conn, season_id)
    return max(clock[1], stored) if clock else stored


# --- games --------------------------------------------------------------------

def locked_teams(conn, season_id: int, ff_week: int) -> set[str]:
    """NFL teams whose game this week is locked."""
    out: set[str] = set()
    for r in conn.execute("SELECT home_team, away_team FROM nfl_game_locks "
                          "WHERE season_id=? AND ff_week=?", (season_id, ff_week)):
        out.update((r["home_team"], r["away_team"]))
    return out


def locked_game_ids(conn, season_id: int, ff_week: int) -> set[str]:
    return {r["game_id"] for r in conn.execute(
        "SELECT game_id FROM nfl_game_locks WHERE season_id=? AND ff_week=?", (season_id, ff_week))}


def games_to_watch(conn, season_id: int, now: _dt.datetime | None = None,
                   window: _dt.timedelta = _dt.timedelta(hours=8)) -> bool:
    """Is any game kicked off within the last `window` and not locked yet?
    The always-on checker only calls ESPN when this is true, so it sleeps
    through the week. A game still unlocked after the window (ESPN couldn't
    settle it) is left to the hourly run and nflverse."""
    from . import settle

    now = now or _dt.datetime.now(ET)
    if settle.follow_up_pending(conn, season_id, now):     # a 30-minute copy still to take
        return True
    for r in conn.execute(
            "SELECT g.ff_week, g.kickoff FROM nfl_week_games g "
            "LEFT JOIN nfl_game_locks k ON k.season_id=g.season_id AND k.ff_week=g.ff_week "
            "AND k.game_id=g.game_id WHERE g.season_id=? AND k.id IS NULL "
            "AND g.kickoff IS NOT NULL", (season_id,)):
        ko = _dt.datetime.fromisoformat(r["kickoff"])
        if ko <= now <= ko + window and not is_finalized(conn, season_id, r["ff_week"]):
            return True
    return False


def bye_teams(conn, season_id: int, ff_week: int) -> set[str]:
    return {r["abbr"] for r in conn.execute(
        "SELECT abbr FROM nfl_teams WHERE season_id=? AND bye_ff_week=?", (season_id, ff_week))}


def store_week_games(conn, season_id: int, ff_week: int, games) -> None:
    """Record the week's games as the schedule has them now: iterable of
    (game_id, home, away, kickoff datetime or None, final score posted)."""
    conn.execute("DELETE FROM nfl_week_games WHERE season_id=? AND ff_week=?",
                 (season_id, ff_week))
    conn.executemany(
        "INSERT INTO nfl_week_games(season_id,ff_week,game_id,home_team,away_team,kickoff,final) "
        "VALUES (?,?,?,?,?,?,?)",
        [(season_id, ff_week, gid, home, away, ko.isoformat() if ko else None, int(bool(fin)))
         for gid, home, away, ko, fin in games])


# --- teams --------------------------------------------------------------------

def _settled(conn, season_id: int, ff_week: int) -> bool:
    """Finalized, or scored before in-progress tracking existed and not flagged."""
    scored = conn.execute("SELECT 1 FROM team_week_scores WHERE season_id=? AND ff_week=? "
                          "AND computed_points IS NOT NULL LIMIT 1",
                          (season_id, ff_week)).fetchone()
    return is_finalized(conn, season_id, ff_week) or (
        scored is not None and ff_week not in live_weeks(conn, season_id))


def _game_context(conn, season_id: int, ff_week: int):
    """(settled, locked teams, bye teams, {team: (kickoff, final score posted)})."""
    games: dict[str, tuple] = {}
    for g in conn.execute("SELECT home_team, away_team, kickoff, final FROM nfl_week_games "
                          "WHERE season_id=? AND ff_week=?", (season_id, ff_week)):
        ko = _dt.datetime.fromisoformat(g["kickoff"]) if g["kickoff"] else None
        for t in (g["home_team"], g["away_team"]):
            games[t] = (ko, bool(g["final"]))
    return (_settled(conn, season_id, ff_week), locked_teams(conn, season_id, ff_week),
            bye_teams(conn, season_id, ff_week), games)


def _state_for(nfl, ctx, now) -> dict:
    """Where one NFL team's game stands — the single rule behind every tag."""
    settled, locked, byes, games = ctx
    ko = None
    if settled or nfl in locked:
        state = "final"
    elif nfl is None:
        state = "nogame"
    elif nfl in byes:
        state = "bye"
    else:
        ko, fin = games.get(nfl, (None, False))
        state = "over" if fin else ("playing" if ko and now >= ko else "upcoming")
    return {"state": state, "kickoff": ko.isoformat() if ko and state == "upcoming" else None}


def team_game_states(conn, season_id: int, ff_week: int,
                     now: _dt.datetime | None = None) -> dict[str, dict]:
    """{NFL team: {"state", "kickoff", "game_at"}} for every NFL team this week
    (see starter_states for what each state means). game_at is the team's
    kickoff whatever the state — for showing a game's day and time."""
    now = now or _dt.datetime.now(ET)
    ctx = _game_context(conn, season_id, ff_week)
    teams = {r["abbr"] for r in conn.execute("SELECT abbr FROM nfl_teams WHERE season_id=?",
                                             (season_id,))} | set(ctx[3]) | ctx[1]
    out = {}
    for t in teams:
        d = _state_for(t, ctx, now)
        ko = ctx[3].get(t, (None, False))[0]
        d["game_at"] = ko.isoformat() if ko else None
        out[t] = d
    return out


def _week_sunday(conn, season_id: int, ff_week: int):
    """The Sunday of a week's NFL slate (Central), or None if kickoffs aren't known."""
    first, _ = kickoffs(conn, season_id, ff_week)
    if first is None:
        return None
    day = first.astimezone(CT).date()
    while day.weekday() != 6:
        day += _dt.timedelta(days=1)
    return day


def counts_visible(conn, season_id: int, ff_week: int, now: _dt.datetime | None = None) -> bool:
    """Whether game cards show "N to play" yet: from noon Central on the week's
    Sunday (commissioner, 2026-09-15). Before then nearly everyone's players are
    still to play, and on a copied lineup the count reads as if the manager had
    submitted one."""
    sunday = _week_sunday(conn, season_id, ff_week)
    if sunday is None:
        return True
    return (now or _dt.datetime.now(ET)) >= _dt.datetime.combine(sunday, _dt.time(12), CT)


def early_lineup_alerts(conn, season_id: int, ff_week: int,
                        now: _dt.datetime | None = None) -> list[dict]:
    """The Thursday banner: for each game before Sunday that hasn't kicked off,
    the teams with no lineup yet and an RB/receiver in that game.
    [{"game": "SF @ LAR", "when": "Thu 7:15 PM", "teams": [{"id", "name"}]}]"""
    from . import display

    now = now or _dt.datetime.now(ET)
    sunday = _week_sunday(conn, season_id, ff_week)
    if sunday is None:
        return []
    games = sorted((_dt.datetime.fromisoformat(g["kickoff"]), g["away_team"], g["home_team"])
                   for g in conn.execute("SELECT home_team, away_team, kickoff FROM nfl_week_games "
                                         "WHERE season_id=? AND ff_week=? AND kickoff IS NOT NULL",
                                         (season_id, ff_week)))
    games = [g for g in games if g[0].astimezone(CT).date() < sunday and now < g[0]]
    if not games:
        return []
    set_lineup = {r["team_id"] for r in conn.execute(
        "SELECT DISTINCT team_id FROM weekly_lineups WHERE season_id=? AND ff_week=?",
        (season_id, ff_week))}
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM teams WHERE season_id=?",
                                                      (season_id,))}
    players = [(r["team_id"], r["nfl_team_abbr"]) for r in conn.execute(
        "SELECT re.team_id, p.nfl_team_abbr FROM roster_entries re "
        "JOIN nfl_players p ON p.season_id=re.season_id AND p.gsis_id=re.asset_ref "
        "WHERE re.season_id=? AND re.asset_kind='PLAYER' AND re.released_ff_week IS NULL",
        (season_id,))]
    out = []
    for ko, away, home in games:
        teams = sorted({t for t, nfl in players if nfl in (home, away) and t not in set_lineup},
                       key=lambda t: names.get(t, ""))
        if teams:
            out.append({"game": f"{display.team(away)} @ {display.team(home)}", "when": _ct_label(ko),
                        "teams": [{"id": t, "name": names.get(t, str(t))} for t in teams]})
    return out


def _ct_label(ko: _dt.datetime) -> str:
    t = ko.astimezone(CT)
    return f"{t.strftime('%a')} {t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def early_players(conn, season_id: int, ff_week: int,
                  now: _dt.datetime | None = None) -> dict[int, list]:
    """{team_id: [(kickoff, name)]} — rostered RBs/receivers whose game is before
    the week's Sunday and hasn't kicked off."""
    now = now or _dt.datetime.now(ET)
    sunday = _week_sunday(conn, season_id, ff_week)
    if sunday is None:
        return {}
    games = {}
    for g in conn.execute("SELECT home_team, away_team, kickoff FROM nfl_week_games "
                          "WHERE season_id=? AND ff_week=?", (season_id, ff_week)):
        if g["kickoff"]:
            ko = _dt.datetime.fromisoformat(g["kickoff"])
            games[g["home_team"]] = games[g["away_team"]] = ko
    early: dict[int, list] = {}
    for r in conn.execute(
            "SELECT re.team_id, p.name, p.nfl_team_abbr FROM roster_entries re "
            "JOIN nfl_players p ON p.season_id=re.season_id AND p.gsis_id=re.asset_ref "
            "WHERE re.season_id=? AND re.asset_kind='PLAYER' AND re.released_ff_week IS NULL",
            (season_id,)):
        ko = games.get(r["nfl_team_abbr"])
        if ko and ko.astimezone(CT).date() < sunday and now < ko:
            early.setdefault(r["team_id"], []).append((ko, r["name"]))
    return {t: sorted(v) for t, v in early.items()}


def lineup_notice(conn, season_id: int, ff_week: int, team_id: int,
                  now: _dt.datetime | None = None) -> dict:
    """What Set Lineup needs to word its banner (commissioner, 2026-09-15):
    early — this team's players whose game is before Sunday and still ahead
    ([{"name", "when"}]); sunday_morning — 8am Central on the week's Sunday has
    passed, when a copied lineup's note turns from quiet to a warning."""
    now = now or _dt.datetime.now(ET)
    sunday = _week_sunday(conn, season_id, ff_week)
    return {"early": [{"name": n, "when": _ct_label(ko)}
                      for ko, n in early_players(conn, season_id, ff_week, now).get(team_id, [])],
            "sunday_morning": sunday is not None
                              and now >= _dt.datetime.combine(sunday, _dt.time(8), CT)}


def lineup_flags(conn, season_id: int, ff_week: int,
                 now: _dt.datetime | None = None) -> dict[int, dict]:
    """{team_id: label} for the lineup week's game cards (commissioner, 2026-09-15).

    Most managers set their lineup Sunday morning, so a label only appears when
    it's worth acting on:
      * no lineup yet, and an RB/receiver on the roster plays BEFORE Sunday
        -> "⚠ Thu player" (hover names them) until that kickoff — starting or
        benching him is decided by then, because at kickoff last week's lineup
        is copied and locks him either way;
      * last week's lineup was copied -> grey "last week's lineup", Sunday
        8am-noon Central only, as the morning reminder;
      * no lineup and nothing to copy -> "⚠ no lineup", Sunday 8am-noon.
    From Sunday noon the cards carry no lineup labels; who never submitted is
    in the commissioner tab."""
    now = now or _dt.datetime.now(ET)
    sunday = _week_sunday(conn, season_id, ff_week)
    if sunday is None:
        return {}
    morning = _dt.datetime.combine(sunday, _dt.time(8), CT)
    noon = _dt.datetime.combine(sunday, _dt.time(12), CT)

    lineup = {r["team_id"]: r["carried"] for r in conn.execute(
        "SELECT team_id, MAX(carried_from IS NOT NULL) carried FROM weekly_lineups "
        "WHERE season_id=? AND ff_week=? GROUP BY team_id", (season_id, ff_week))}
    early = early_players(conn, season_id, ff_week, now)

    out = {}
    for tid in [r["id"] for r in conn.execute("SELECT id FROM teams WHERE season_id=?", (season_id,))]:
        if tid in lineup and not lineup[tid]:
            continue                                        # the manager submitted
        if tid in lineup:
            if morning <= now < noon:
                out[tid] = {"kind": "carried", "text": "last week's lineup",
                            "title": "No lineup submitted yet — last week's lineup is in place"}
            continue
        if tid in early:
            players = early[tid]
            day = players[0][0].astimezone(CT).strftime("%a")
            out[tid] = {"kind": "early", "text": f"{day} player{'s' if len(players) > 1 else ''}",
                        "title": ", ".join(f"{name} ({_ct_label(ko)})" for ko, name in players)
                                 + " — set the lineup before kickoff"}
        elif morning <= now < noon:
            out[tid] = {"kind": "none", "text": "no lineup", "title": "No lineup set"}
    return out


def starter_states(conn, season_id: int, ff_week: int, team_id: int,
                   now: _dt.datetime | None = None) -> dict[tuple[str, str], dict]:
    """{(roster_slot, asset_ref): {"state", "kickoff"}} for one team's starters.

    state: final     — his game's stats are locked (or the week is settled);
           playing   — his game has kicked off, not over yet;
           over      — final score posted, stats not locked yet ("scoring soon");
           upcoming  — hasn't kicked off (kickoff is its ISO time, if known);
           bye       — his NFL team has no game this week;
           nogame    — he isn't on an NFL team we know of.
    Every starter who isn't final has a state other than "final", so a row the
    site leaves untagged is finished — including one that scored 0."""
    now = now or _dt.datetime.now(ET)
    ctx = _game_context(conn, season_id, ff_week)
    return {(r["roster_slot"], r["asset_ref"]): _state_for(r["nfl"], ctx, now) for r in conn.execute(
        "SELECT l.roster_slot, l.asset_ref, "
        "CASE WHEN l.asset_kind='TEAM_UNIT' THEN l.asset_ref ELSE p.nfl_team_abbr END nfl "
        "FROM weekly_lineups l "
        "LEFT JOIN nfl_players p ON p.season_id=l.season_id AND p.gsis_id=l.asset_ref "
        "WHERE l.season_id=? AND l.ff_week=? AND l.team_id=?",
        (season_id, ff_week, team_id))}


def statuses(conn, season_id: int, ff_week: int,
             now: _dt.datetime | None = None) -> dict[int, dict]:
    """{team_id: status} for every team with at least one starter this week.

    status: starters, has_lineup (all 9 set), to_play (starters whose game
    isn't locked yet), open (starters with no game — on bye), floor (points
    locked in), adjusted (commissioner-overridden total), done."""
    now = now or _dt.datetime.now(ET)
    settled = _settled(conn, season_id, ff_week)
    locked = locked_teams(conn, season_id, ff_week)
    byes = bye_teams(conn, season_id, ff_week)
    _, last = kickoffs(conn, season_id, ff_week)
    week_over = last is not None and now >= last
    adjusted = {r["team_id"] for r in conn.execute(
        "SELECT team_id FROM team_week_scores WHERE season_id=? AND ff_week=? AND adjusted=1",
        (season_id, ff_week))}

    out: dict[int, dict] = {}
    for r in conn.execute(
            "SELECT l.team_id, "
            "CASE WHEN l.asset_kind='TEAM_UNIT' THEN l.asset_ref ELSE p.nfl_team_abbr END nfl, "
            "COALESCE(a.points, 0) pts FROM weekly_lineups l "
            "LEFT JOIN nfl_players p ON p.season_id=l.season_id AND p.gsis_id=l.asset_ref "
            "LEFT JOIN asset_week_scores a ON a.season_id=l.season_id AND a.ff_week=l.ff_week "
            "AND a.asset_kind=l.asset_kind AND a.asset_ref=l.asset_ref "
            "AND (l.asset_kind='PLAYER' OR a.unit_type=l.roster_slot) "
            "WHERE l.season_id=? AND l.ff_week=?", (season_id, ff_week)):
        s = out.setdefault(r["team_id"], {"starters": 0, "to_play": 0, "open": 0, "floor": 0.0})
        s["starters"] += 1
        if r["nfl"] in locked:
            s["floor"] += r["pts"]
        elif r["nfl"] is None or r["nfl"] in byes:
            s["open"] += 1
        else:
            s["to_play"] += 1

    for tid, s in out.items():
        s["has_lineup"] = s["starters"] >= 9
        s["adjusted"] = tid in adjusted
        s["settled"] = settled
        if settled:
            s["to_play"], s["done"] = 0, True
        else:
            s["done"] = (s["has_lineup"] and s["to_play"] == 0
                         and (s["open"] == 0 or week_over))
    return out
