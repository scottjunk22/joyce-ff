"""Site traffic from the access log (Scott, 2026-09-29): counts only, the live
league only, no bots — and a box score counted once, not once per team."""

from datetime import datetime, timezone

from joyce_ff.league import sitestats

PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) Safari/604.1"
DESK = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/156.0"
BOT = "Mozilla/5.0 (compatible; Googlebot/2.1)"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def line(ip, when, method, path, status, agent):
    return (f'{ip} - - [{when}] "{method} {path} HTTP/1.1" {status} 12 '
            f'"https://www.stevejoyceff.com/" "{agent}" "{ip}" response-time=0.1')


def at(hms):
    return f"27/Sep/2026:{hms} +0000"


LOG = [
    line("1.1.1.1", at("17:05:00"), "GET", "/", 200, PHONE),
    line("1.1.1.1", at("17:05:01"), "GET", "/api/state?week=1&season=2", 200, PHONE),
    # a box score: both teams, same week, a second apart = ONE
    line("1.1.1.1", at("17:06:00"), "GET", "/api/team/38/detail?week=1&season=2", 200, PHONE),
    line("1.1.1.1", at("17:06:01"), "GET", "/api/team/27/detail?week=1&season=2", 200, PHONE),
    # a roster card on its own
    line("1.1.1.1", at("17:20:00"), "GET", "/api/team/38/detail?week=1&season=2", 200, PHONE),
    line("2.2.2.2", at("17:30:00"), "GET", "/api/state?week=1", 200, DESK),
    # two single-team looks for different weeks, a second apart: not a pair
    line("2.2.2.2", at("17:30:30"), "GET", "/api/team/5/detail?week=1&season=2", 200, DESK),
    line("2.2.2.2", at("17:30:31"), "GET", "/api/team/9/detail?week=2&season=2", 200, DESK),
    line("2.2.2.2", at("17:31:00"), "POST", "/api/team/5/lineup", 200, DESK),
    line("2.2.2.2", at("17:31:30"), "POST", "/api/team/5/lineup", 403, DESK),   # wrong PIN
    line("2.2.2.2", at("17:40:00"), "POST", "/api/team/5/trade", 200, DESK),
    line("3.3.3.3", at("17:50:00"), "GET", "/dark/api/state", 200, DESK),       # practice
    line("3.3.3.3", at("17:51:00"), "GET", "/api/otblitz/board", 200, DESK),    # private
    line("4.4.4.4", at("17:52:00"), "GET", "/", 200, BOT),                       # bot
    line("1.1.1.1", "01/Sep/2026:17:00:00 +0000", "GET", "/", 200, PHONE),       # too old
    "garbage line",
]


def test_it_counts_people_looking_at_scores_and_nothing_else():
    s = sitestats.summarize(sitestats.parse(LOG), days=7, now=NOW)
    t = s["totals"]
    assert (t["visit"], t["scoreboard"], t["box"], t["roster"]) == (1, 2, 1, 3)
    assert (t["lineup"], t["trade"], t["open"]) == (1, 1, 0)      # the 403 isn't a save
    assert s["visitors"] == 2 and s["phones"] == 1                 # practice, private, bot left out
    (day,) = s["days"]
    assert str(day) == "2026-09-27"                                # 17:05 UTC is Sunday noon CT
    assert s["busiest"][0][0] == ("Sun", 12)
    text = sitestats.report(s)
    assert "1.1.1.1" not in text and "2.2.2.2" not in text         # counts, never addresses


def test_the_spread_shows_one_heavy_user_for_what_it_is():
    heavy = [line("9.9.9.9", at(f"18:{m:02d}:0{h}"), "GET",
                  f"/api/team/{10 + h}/detail?week=1", 200, DESK)
             for m in range(10) for h in (0, 1)]                    # 10 box scores
    light = [line(f"8.8.8.{n}", at("19:00:00"), "GET", "/api/team/1/detail?week=1", 200, PHONE)
             for n in range(4)] + \
            [line(f"8.8.8.{n}", at("19:00:01"), "GET", "/api/team/2/detail?week=1", 200, PHONE)
             for n in range(4)]                                     # 1 each
    s = sitestats.summarize(sitestats.parse(heavy + light), days=7, now=NOW)
    assert s["totals"]["box"] == 14 and s["totals"]["roster"] == 0
    assert s["box_week"] == {"visitors": 5, "top": [10, 1, 1], "median": 1}
    assert "busiest 10, 1, 1 · median 1" in sitestats.report(s)
