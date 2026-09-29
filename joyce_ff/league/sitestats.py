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
  * box scores   a team opened from the scoreboard or standings (the same
                 request serves a box score, a roster card and Set Lineup);
  * lineup saves, trades and Opens that went through.

Nobody's address is printed — only counts.
"""

from __future__ import annotations

import glob
import gzip
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
LOG_GLOB = "/var/log/www.stevejoyceff.com.access.log*"

_LINE = re.compile(r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d{3}) \S+ "[^"]*" "([^"]*)"')
_BOT = re.compile(r"bot|crawl|spider|slurp|preview|monitor|curl|python|wget|headless", re.I)
_TEAM = re.compile(r"^/api/team/\d+/(detail|lineup|trade|open)$")
COLUMNS = ("visit", "scoreboard", "team", "lineup", "trade", "open")


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
    """What a request means to the league, or None for anything not counted."""
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
    what = m.group(1)
    if what == "detail":
        return "team" if method == "GET" else None
    return what if method == "POST" else None


def device(agent: str) -> str:
    return "phone" if re.search(r"iPhone|Android|Mobile|iPad", agent) else "computer"


def summarize(records, days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    by_day = defaultdict(lambda: {"visitors": set(), **{c: 0 for c in COLUMNS}})
    hours = Counter()
    visitors, devices = set(), {}
    totals = Counter()
    for when, ip, method, path, status, agent in records:
        if when < since or status >= 400 or _BOT.search(agent):
            continue                     # only what went through, and only people
        k = kind(method, path)
        if k is None:
            continue
        local = when.astimezone(CT)
        who = (ip, agent)
        day = by_day[local.date()]
        day["visitors"].add(who)
        day[k] += 1
        visitors.add(who)
        devices[who] = device(agent)
        totals[k] += 1
        if k in ("scoreboard", "team"):
            hours[(local.strftime("%a"), local.hour)] += 1
    return {"since": since, "days": dict(sorted(by_day.items())), "visitors": len(visitors),
            "phones": sum(1 for d in devices.values() if d == "phone"),
            "totals": totals, "busiest": hours.most_common(5)}


def read_logs(pattern: str = LOG_GLOB):
    for path in sorted(glob.glob(pattern)):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                yield from fh
        except OSError:
            continue


def report(s: dict) -> str:
    head = ("visitors", "visits", "scoreboards", "box scores", "lineups", "trades", "Opens")
    out = [f"Since {s['since'].astimezone(CT):%a %b %d, %I:%M %p} CT — live league only, no bots", "",
           f"{'':11}" + "".join(f"{h:>12}" for h in head)]
    for day, d in s["days"].items():
        out.append(f"{day:%a %b %d} " + f"{len(d['visitors']):>12}"
                   + "".join(f"{d[c]:>12}" for c in COLUMNS))
    t = s["totals"]
    out += ["",
            f"{s['visitors']} different visitors, {s['phones']} of them on a phone",
            f"{t['visit']} visits · {t['scoreboard']} scoreboard views · {t['team']} box scores or rosters opened",
            f"{t['lineup']} lineups saved · {t['trade']} trades · {t['open']} Opens",
            "", "Busiest hours for watching scores (CT):"]
    for (dow, hr), n in s["busiest"]:
        out.append(f"  {dow} {datetime(2000, 1, 1, hr).strftime('%I %p').lstrip('0'):>5}   {n}")
    out += ["", "A visitor is one browser at one address, so a phone and a laptop count twice."]
    return "\n".join(out)
