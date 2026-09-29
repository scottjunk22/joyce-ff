"""Site traffic from the access log (Scott, 2026-09-29): counts only, the live
league only, and no bots."""

from datetime import datetime, timezone

from joyce_ff.league import sitestats

PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) Safari/604.1"
DESK = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/156.0"
BOT = "Mozilla/5.0 (compatible; Googlebot/2.1)"


def line(ip, when, method, path, status, agent):
    return (f'{ip} - - [{when}] "{method} {path} HTTP/1.1" {status} 12 '
            f'"https://www.stevejoyceff.com/" "{agent}" "{ip}" response-time=0.1')


LOG = [
    line("1.1.1.1", "27/Sep/2026:17:05:00 +0000", "GET", "/", 200, PHONE),
    line("1.1.1.1", "27/Sep/2026:17:05:01 +0000", "GET", "/api/state?week=1&season=2", 200, PHONE),
    line("1.1.1.1", "27/Sep/2026:17:06:00 +0000", "GET", "/api/team/38/detail?week=1", 200, PHONE),
    line("2.2.2.2", "27/Sep/2026:17:30:00 +0000", "GET", "/api/state?week=1", 200, DESK),
    line("2.2.2.2", "27/Sep/2026:17:31:00 +0000", "POST", "/api/team/5/lineup", 200, DESK),
    line("2.2.2.2", "27/Sep/2026:17:31:30 +0000", "POST", "/api/team/5/lineup", 403, DESK),   # wrong PIN
    line("2.2.2.2", "27/Sep/2026:17:40:00 +0000", "POST", "/api/team/5/trade", 200, DESK),
    line("3.3.3.3", "27/Sep/2026:17:50:00 +0000", "GET", "/dark/api/state", 200, DESK),       # practice
    line("3.3.3.3", "27/Sep/2026:17:51:00 +0000", "GET", "/api/otblitz/board", 200, DESK),    # private
    line("4.4.4.4", "27/Sep/2026:17:52:00 +0000", "GET", "/", 200, BOT),                       # bot
    line("1.1.1.1", "01/Sep/2026:17:00:00 +0000", "GET", "/", 200, PHONE),                     # too old
    "garbage line",
]


def test_it_counts_people_looking_at_scores_and_nothing_else():
    s = sitestats.summarize(sitestats.parse(LOG), days=7,
                            now=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc))
    t = s["totals"]
    assert (t["visit"], t["scoreboard"], t["team"]) == (1, 2, 1)
    assert (t["lineup"], t["trade"], t["open"]) == (1, 1, 0)      # the 403 isn't a save
    assert s["visitors"] == 2 and s["phones"] == 1                 # practice, private, bot left out
    (day,) = s["days"]
    assert str(day) == "2026-09-27"                                # 17:05 UTC is Sunday noon CT
    assert s["busiest"][0][0] == ("Sun", 12)
    text = sitestats.report(s)
    assert "1.1.1.1" not in text and "2.2.2.2" not in text         # counts, never addresses
