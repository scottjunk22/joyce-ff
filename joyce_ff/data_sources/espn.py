"""
ESPN's game feed — the fast source for weekly scoring.

ESPN's site and apps read public JSON feeds with full box scores that are
complete within minutes of the final whistle. nflverse, validated but slow (its
scheduled updates run hours late, get skipped, or fail), stays as the backup
and as the later cross-check of whatever ESPN locked.

It's an unofficial feed: no key, no documentation, and ESPN can change it
without notice. So every read here fails loudly (Unavailable) rather than
quietly returning something half-parsed, and the scoring layer only locks a
game from ESPN when it understood all of it.

Checked against nflverse on three full 2026 Week 1 games (BUF-HOU, CHI-CAR,
NO-DET): every team-unit stat identical, 65 of 66 players identical, and the one
difference was lateral yards our nflverse reader had been dropping.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field

# Two hosts serve the same feed. From PythonAnywhere, ESPN's edge (Akamai)
# refuses site.api.espn.com with a 403 while site.web.api.espn.com answers;
# both return identical data (checked on the scoreboard and four box scores,
# 2026-09-14). Try them in order so one being blocked isn't an outage.
HOSTS = ("site.web.api.espn.com", "site.api.espn.com")
PATH = "/apis/site/v2/sports/football/nfl"
# Plain on purpose: ESPN's edge rejects (403) a user agent with a descriptive
# suffix — "Mozilla/5.0 (private fantasy league scoring)" was refused, plain
# "Mozilla/5.0" accepted (2026-09-13).
UA = {"User-Agent": "Mozilla/5.0"}

# ESPN -> the schedule's spelling (nflverse, and so our database).
TEAM_ALIASES = {"WSH": "WAS", "LAR": "LA"}

# Scoring-play types, as ESPN labels them. Anything else means we don't fully
# understand the game, and it isn't locked from ESPN.
_PLAIN = {"Rushing Touchdown", "Passing Touchdown", "Field Goal Good"}
_DEFENSIVE_TD = {"Interception Return Touchdown", "Fumble Return Touchdown",
                 "Sack Opp Fumble Recovery"}
_SPECIAL_TEAMS_TD = {"Punt Return Touchdown", "Kickoff Return Touchdown",
                     "Blocked Field Goal", "Blocked Punt", "Blocked Punt Touchdown",
                     "Blocked Field Goal Touchdown", "Missed Field Goal Return Touchdown"}


# A conversion rides along in a scoring play's text, e.g.
#   "... (Carson Wentz Pass to Justin Jefferson for Two-Point Conversion)"
#   "... (Saquon Barkley Run for Two-Point Conversion)"
#   "... (Two-Point Pass Conversion Failed)"
_TWO_POINT = re.compile(r"\(([^()]*Two-Point[^()]*)\)", re.I)
_TWO_POINT_PASS = re.compile(r"^(.+?) Pass to (.+?) for Two-Point Conversion$", re.I)
_TWO_POINT_RUN = re.compile(r"^(.+?) (?:Run|Rush) for Two-Point Conversion$", re.I)


class Unavailable(RuntimeError):
    """ESPN didn't answer, or answered in a shape we don't recognise."""


def team(abbr: str) -> str:
    return TEAM_ALIASES.get(abbr, abbr)


def _get(url: str, timeout: int = 30) -> dict:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:  # network, HTTP, or not JSON
        raise Unavailable(f"ESPN request failed: {e}") from e


def _fetch(path: str) -> dict:
    """`path` from the first ESPN host that answers."""
    errors = []
    for host in HOSTS:
        try:
            return _get(f"https://{host}{PATH}/{path}")
        except Unavailable as e:
            errors.append(f"{host}: {e}")
    raise Unavailable("; ".join(errors))


@dataclass
class Event:
    event_id: str
    game_id: str       # nflverse-style id, built from the teams
    home: str
    away: str
    state: str         # pre / in / post
    completed: bool


def week_events(year: int, nfl_week: int, seasontype: int = 2) -> list[Event]:
    """Every game ESPN lists for a regular-season NFL week."""
    sb = _fetch(f"scoreboard?seasontype={seasontype}&week={nfl_week}&dates={year}")
    out = []
    try:
        for e in sb["events"]:
            sides = {c["homeAway"]: team(c["team"]["abbreviation"])
                     for c in e["competitions"][0]["competitors"]}
            st = e["status"]["type"]
            out.append(Event(str(e["id"]), f"{year}_{nfl_week:02d}_{sides['away']}_{sides['home']}",
                             sides["home"], sides["away"], st["state"], bool(st["completed"])))
    except (KeyError, IndexError, TypeError) as e:
        raise Unavailable(f"ESPN scoreboard wasn't in the expected shape: {e}") from e
    return out


@dataclass
class GameLines:
    """One game, reduced to exactly what our scoring reads."""
    players: dict[str, dict] = field(default_factory=dict)   # ESPN athlete id -> stats
    units: dict[str, dict] = field(default_factory=dict)     # team -> team-unit stats
    unknown_scoring: list[str] = field(default_factory=list)  # play types we can't classify


def _num(v) -> float:
    s = str(v).split("/")[0].strip()
    return float(s) if s not in ("", "--", "None") else 0.0


def game_lines(event_id: str) -> GameLines:
    s = _fetch(f"summary?event={event_id}")
    try:
        return _parse(s)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
        raise Unavailable(f"ESPN box score for {event_id} wasn't in the expected shape: {e}") from e


def _parse(s: dict) -> GameLines:
    bx = s["boxscore"]
    g = GameLines()

    def cat(block, name):
        for grp in block.get("statistics", []):
            if grp["name"] == name:
                return [(str(a["athlete"]["id"]), a["athlete"]["displayName"],
                         dict(zip(grp["labels"], a["stats"]))) for a in grp["athletes"]]
        return []

    blocks = {team(b["team"]["abbreviation"]): b for b in bx["players"]}
    for ab, b in blocks.items():
        for c, keys in (("passing", {"TD": "passing_tds"}),
                        ("rushing", {"YDS": "rushing_yards", "TD": "rushing_tds"}),
                        ("receiving", {"REC": "receptions", "YDS": "receiving_yards",
                                       "TD": "receiving_tds"}),
                        ("kickReturns", {"TD": "return_tds"}),
                        ("puntReturns", {"TD": "return_tds"})):
            for aid, name, st in cat(b, c):
                p = g.players.setdefault(aid, {"name": name, "team": ab})
                for label, f in keys.items():
                    p[f] = p.get(f, 0) + _num(st.get(label, 0))

    tstats = {team(t["team"]["abbreviation"]): {x["name"]: x.get("displayValue") for x in t["statistics"]}
              for t in bx["teams"]}
    score = {team(c["team"]["abbreviation"]): int(_num(c["score"]))
             for c in s["header"]["competitions"][0]["competitors"]}
    sides = list(score)
    opp = {sides[0]: sides[1], sides[1]: sides[0]}

    for ab in sides:
        b = blocks.get(ab, {})
        g.units[ab] = {
            # NET passing yards (sacks subtracted) — the league's rule. A
            # passer's YDS in the box score is gross, so use the team's own
            # net figure.
            "passing_yards": max(0.0, _num(tstats[ab]["netPassingYards"])),
            "passing_tds": sum(_num(st["TD"]) for _, _, st in cat(b, "passing")),
            "fg_distances": [],
            "extra_points_made": sum(_num(st["XP"]) for _, _, st in cat(b, "kicking")),
            "points_allowed": score[opp[ab]],
            "yards_allowed": _num(tstats[opp[ab]]["totalYards"]),
            "sacks": sum(_num(st["SACKS"]) for _, _, st in cat(b, "defensive")),
            "interceptions": sum(_num(st["INT"]) for _, _, st in cat(b, "interceptions")),
            "fumble_recoveries": _num(tstats[opp[ab]]["fumblesLost"]),
            "two_point_passes": 0,
            "safeties": 0, "defensive_tds": 0, "special_teams_tds": 0,
            "won": score[ab] > score[opp[ab]], "tied": score[ab] == score[opp[ab]],
        }

    def credit_conversion(ab, name):
        """+1 two-point conversion for the named scorer. A player whose only
        stat is the conversion isn't in the box score, so he's keyed by name
        and matched to ours by name and team."""
        for p in g.players.values():
            if p["team"] == ab and p["name"] == name:
                p["two_point_conversions"] = p.get("two_point_conversions", 0) + 1
                return
        p = g.players.setdefault(f"name:{ab}:{name}", {"name": name, "team": ab})
        p["two_point_conversions"] = p.get("two_point_conversions", 0) + 1

    for p in s.get("scoringPlays", []):
        ab, kind = team(p["team"]["abbreviation"]), p["type"]["text"]
        u = g.units[ab]
        conv = _TWO_POINT.search(p.get("text", ""))
        if conv and "fail" not in conv.group(1).lower():
            text = conv.group(1).strip()
            passed, ran = _TWO_POINT_PASS.match(text), _TWO_POINT_RUN.match(text)
            if passed:
                u["two_point_passes"] += 1
                credit_conversion(ab, passed.group(2).strip())
            elif ran:
                credit_conversion(ab, ran.group(1).strip())
            else:
                g.unknown_scoring.append(f"two-point conversion: {text}")
        if kind == "Field Goal Good":
            m = re.search(r"(\d+) Yd Field Goal", p.get("text", ""))
            if not m:
                g.unknown_scoring.append(f"{kind}: {p.get('text')}")
                continue
            u["fg_distances"].append(int(m.group(1)))
        elif kind in _PLAIN:
            continue
        elif "Safety" in kind:
            u["safeties"] += 1
        elif kind in _DEFENSIVE_TD:
            u["defensive_tds"] += 1
        elif kind in _SPECIAL_TEAMS_TD:
            u["special_teams_tds"] += 1
        else:
            g.unknown_scoring.append(kind)
    return g
