"""
Print a team's full fantasy schedule for a season.

Matchups come from the fixed Team# rotation; dates come from the real NFL
schedule (nflverse). Since 2026-27 Team #s are drawn on draft day, pass the
Team # you drew (or try any) to see the slate.

Run: python manage.py schedule --conf BLUE --team 4 --season 2026
"""

from __future__ import annotations

import sys

from joyce_ff.schedule import calendar as cal
from joyce_ff.schedule import rotation as rot


def _arg(argv, flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv=None) -> int:
    argv = argv or []
    conf = str(_arg(argv, "--conf", "BLUE")).upper()
    season = int(_arg(argv, "--season", 2026))
    team = _arg(argv, "--team")

    rot.validate_rotation()  # fail loudly if the table is ever corrupted

    if team is None:
        print("Specify --team N (1-11). Your Team # is drawn on draft day.\n"
              "Example: python manage.py schedule --conf BLUE --team 4 --season 2026")
        return 1
    team = int(team)

    calmap = cal.season_calendar(season)
    sched = rot.team_schedule(team, conf)

    print(f"\n{conf} Team #{team} — {season}-{str(season+1)[2:]} fantasy schedule")
    print(f"(matchups from the Team# rotation; dates from the real NFL schedule; "
          f"assumes FF Week 1 = NFL Week {cal.FF_START_NFL_WEEK})")
    print("-" * 60)
    print(f"{'FF':>3} {'NFL':>4}  {'dates':13}  opponent")
    for g in sched:
        ff = g["ff_week"]
        c = calmap[ff]
        if g["kind"] == "no_play":
            opp = "— No Play (NFL Week 18) —"
        elif g["kind"] == "bye":
            opp = "BYE"
        elif g["kind"] == "interleague":
            opp = f"vs {g['opp_conf']} #{g['opp_team']}  (interleague)"
        else:
            opp = f"vs {g['opp_conf']} #{g['opp_team']}"
        star = " *" if g["kind"] == "bye" else ""
        print(f"{ff:>3} {c['nfl_week']:>4}  {c['label']:13}  {opp}{star}")

    byes = [g["ff_week"] for g in sched if g["kind"] == "bye"]
    print("-" * 60)
    print(f"Bye: FF Week {byes[0] if byes else '—'}. "
          f"15 games (FF Weeks 1-4 interleague, 5-15 conference).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
