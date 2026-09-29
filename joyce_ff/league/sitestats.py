"""
How busy the league site has been, read from the web server's access log.

PythonAnywhere keeps one line per request — time, path, status and browser —
in /var/log/<domain>.access.log, rotated into numbered (and gzipped) files.
That's enough to answer "are people using it, and are they watching scores?"
without adding any tracking to the site itself (Scott, 2026-09-29).

What it counts, on the LIVE league only — the practice copy (/dark) and the
private OT-Blitz pages are left out, and so are bots:

  * visitors     distinct browser + address pairs: a household sharing one
                 phone is one visitor, a manager on phone and laptop is two;
  * visits       the site opened (the home page loaded);
  * scoreboards  the scoreboard fetched — on opening, on every week switch,
                 and after a save refreshes it;
  * box scores   a game tapped on the scoreboard. The page loads BOTH teams at
                 once, so the log holds two team lines a second apart; a pair
                 from one visitor is ONE box score. (The first version counted
                 lines and doubled every box score — Scott caught it.)
  * rosters      a team line with no partner: a roster card, Set Lineup or the
                 trade screen, which the log can't tell apart;
  * lineup saves, trades and Opens that went through.

Box scores per visitor are shown as a spread (how many opened any, the
busiest few, the median) so one heavy user — the commissioner testing, say —
can be seen for what it is. Nobody's address is printed — only counts.
"""

from __future__ import annotations

import glob
import gzip
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
LOG_GLOB = "/var/log/www.stevejoyceff.com.access.log*"
PAIR_SECONDS = 2          # the two halves of a box score arrive together

_LINE = re.compile(r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d{3}) \S+ "[^"]*" "([^"]*)"')
_BOT = re.compile(r"bot|crawl|spider|slurp|preview|monitor|curl|python|wget|headless", re.I)
_TEAM = re.compile(r"^/api/team/(\d+)/(detail|lineup|trade|open)$")
COLUMNS = ("visit", "scoreboard", "box", "roster", "lineup", "trade", "open")


def parse(lines):
    """Yield (when UTC, ip, method, path, status, agent) for each line that
    parses; anything else is skipped rather than guessed at."""
    for line in lines:
        m = _LINE.match(line)
        if not m:
            continue
        ip, ts, method, path, status, agent = m.groups()
        try:
            when = datetime.strptime(ts, "%d/%b/%Y:%H:%M:%S %z")
        except ValueError:
            continue
        yield when, ip, method, path, int(status), agent


def kind(method: str, path: str) -> str | None:
    """What a request means to the league, or None for anything not counted.
    A team's detail is "team" here; summarize() decides box score or roster."""
    bare = path.split("?", 1)[0]
    if bare.startswith(("/dark", "/otblitz", "/api/otblitz", "/static")):
        return None
    if method == "GET" and bare == "/":
        return "visit"
    if method == "GET" and bare == "/api/state":
        return "scoreboard"
    m = _TEAM.match(bare)
    if not m:
        return None
    what = m.group(2)
    if what == "detail":
        return "team" if method == "GET" else None
    return what if method == "POST" else None


def device(agent: str) -> str:
    return "phone" if re.search(r"iPhone|Android|Mobile|iPad", agent) else "computer"


def _pair_up(loads):
    """One visitor's team loads, as (when, team id, query) -> [(when, "box" |
    "roster")]. Two different teams for the same week within PAIR_SECONDS are
    one box score; anything left over is a roster-type look."""
    loads = sorted(loads)
    out, i = [], 0
    while i < len(loads):
        when, team, query = loads[i]
        if i + 1 < len(loads):
            nwhen, nteam, nquery = loads[i + 1]
            if (nteam != team and nquery == query
                    and (nwhen - when).total_seconds() <= PAIR_SECONDS):
                out.append((when, "box"))
                i += 2
                continue
        out.append((when, "roster"))
        i += 1
    return out


def _spread(per_visitor: Counter) -> dict:
    counts = sorted(per_visitor.values(), reverse=True)
    return {"visitors": len(counts), "top": counts[:3],
            "median": median(counts) if counts else 0}


def summarize(records, days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    events = []                                  # (when, who, kind)
    loads = defaultdict(list)                    # who -> team loads, paired below
    devices = {}
    for when, ip, method, path, status, agent in records:
        if when < since or status >= 400 or _BOT.search(agent):
            continue                     # only what went through, and only people
        k = kind(method, path)
        if k is None:
            continue
        who = (ip, agent)
        devices[who] = device(agent)
        if k == "team":
            bare, _, query = path.partition("?")
            loads[who].append((when, _TEAM.match(bare).group(1), query))
        else:
            events.append((when, who, k))
    for who, mine in loads.items():
        events += [(when, who, k) for when, k in _pair_up(mine)]

    by_day = defaultdict(lambda: {"visitors": set(), **{c: 0 for c in COLUMNS}})
    boxes_by_day = defaultdict(Counter)          # day -> who -> box scores
    boxes = Counter()
    hours, totals = Counter(), Counter()
    for when, who, k in events:
        local = when.astimezone(CT)
        day = by_day[local.date()]
        day["visitors"].add(who)
        day[k] += 1
        totals[k] += 1
        if k == "box":
            boxes_by_day[local.date()][who] += 1
            boxes[who] += 1
        if k in ("scoreboard", "box", "roster"):
            hours[(local.strftime("%a"), local.hour)] += 1
    return {"since": since, "days": dict(sorted(by_day.items())),
            "visitors": len(devices),
            "phones": sum(1 for d in devices.values() if d == "phone"),
            "totals": totals, "busiest": hours.most_common(5),
            "box_spread": {d: _spread(c) for d, c in sorted(boxes_by_day.items())},
            "box_week": _spread(boxes)}


def read_logs(pattern: str = LOG_GLOB):
    for path in sorted(glob.glob(pattern)):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                yield from fh
        except OSError:
            continue


def _spread_text(sp: dict) -> str:
    med = sp["median"]
    med = int(med) if med == int(med) else round(med, 1)
    return (f"{sp['visitors']} visitors · busiest {', '.join(map(str, sp['top']))}"
            f" · median {med}")


def report(s: dict) -> str:
    head = ("visitors", "visits", "scoreboards", "box scores", "rosters", "lineups", "trades", "Opens")
    out = [f"Since {s['since'].astimezone(CT):%a %b %d, %I:%M %p} CT — live league only, no bots", "",
           f"{'':11}" + "".join(f"{h:>12}" for h in head)]
    for day, d in s["days"].items():
        out.append(f"{day:%a %b %d} " + f"{len(d['visitors']):>12}"
                   + "".join(f"{d[c]:>12}" for c in COLUMNS))
    t = s["totals"]
    out += ["",
            f"{s['visitors']} different visitors, {s['phones']} of them on a phone",
            f"{t['visit']} visits · {t['scoreboard']} scoreboard views · {t['box']} box scores"
            f" · {t['roster']} rosters / Set Lineup / trade screens",
            f"{t['lineup']} lineups saved · {t['trade']} trades · {t['open']} Opens",
            "", "Box scores per visitor (busiest three, then the middle visitor):"]
    for day, sp in s["box_spread"].items():
        out.append(f"  {day:%a %b %d}    {_spread_text(sp)}")
    out.append(f"  whole period  {_spread_text(s['box_week'])}")
    out += ["", "Busiest hours for watching scores (CT):"]
    for (dow, hr), n in s["busiest"]:
        out.append(f"  {dow} {datetime(2000, 1, 1, hr).strftime('%I %p').lstrip('0'):>5}   {n}")
    out += ["", "A visitor is one browser at one address, so a phone and a laptop count twice.",
            "A box score is one game tapped (both teams); rosters are single-team looks."]
    return "\n".join(out)
