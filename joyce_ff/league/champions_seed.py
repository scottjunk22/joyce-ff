"""
The league's Super Bowl winners, as supplied by the commissioner's site.

Transcribed VERBATIM, including spelling that drifts across the years
("Shuffling Crew" / "ShufflingCrew", "Tall Boys" / "TallBoys", "MuddyChicks" /
"Muddy Chickens") and the manager names some entries carry in parentheses.
The record should match his, so nothing here is tidied up or second-guessed.

YEARS ARE THE SEASON'S ENDING YEAR as he lists them — his "2026" is the
2025-26 season. They are stored by STARTING year to match `seasons.year`
elsewhere, so 2026 goes in as 2025 and renders as "2025-26".

36 seasons: 1990-91 (his 1991) through 2025-26 (his 2026).
"""

from __future__ import annotations

# (ending year as listed, champion, runner-up)
CHAMPIONS = [
    (2026, "Ribears", "Pike"),
    (2025, "ShufflingCrew", "TallBoys"),
    (2024, "TallBears", "Ribears"),
    (2023, "OTBlitz", "BGH"),
    (2022, "MuddyChicks", "BarnBrners"),
    (2021, "Cooper", "Hellman"),
    (2020, "Hellman", "BarnBurners"),
    (2019, "Chaos", "BarnBurners"),
    (2018, "Juggernuts", "Mooners"),
    (2017, "BGH", "D&D"),
    (2016, "TKatich", "Mooners"),
    (2015, "D&D", "Ribears"),
    (2014, "Bad Boys", "B-ski"),
    (2013, "BarnBurners", "BGH"),
    (2012, "Pack", "Muddy Chickens"),
    (2011, "Polecats", "Pike"),
    (2010, "BarnBurners", "Shuffling Crew"),
    (2009, "Wild Bill", "Hammertime"),
    (2008, "Smith", "Pike"),
    (2007, "Hammertime", "Tall Boys"),
    (2006, "Ribears", "Pigskin Punks"),
    (2005, "Pack", "Shuffling Crew"),
    (2004, "Burns Bruisers", "Shuffling Crew"),
    (2003, "Mud Dobbers", "Ribears"),
    (2002, "Stars (Raziq)", "D&D"),
    (2001, "Punks", "Fox"),
    (2000, "QB Club (Sievers)", "Refs"),
    (1999, "RIP (Tallmans)", "Dawgs (Neuhaus)"),
    (1998, "Sparky's Return (Tiburzi)", "TT"),
    (1997, "Low Riders", "NFL (Joyce)"),
    (1996, "Low Riders", "Bad Boys"),
    (1995, "Bad Boys", "Pack"),
    (1994, "Bad Boys", "No Names (Thompson)"),
    (1993, "McHay (McDaniels)", "Punks"),
    (1992, "Sweethings II (Allan)", "Punks"),
    (1991, "Macs (McDaniels)", "Band-its (Kay)"),
]


def rows() -> list[tuple[int, str, str, str]]:
    """(start_year, label, champion, runner_up) ready for the champions table."""
    out = []
    for end_year, champ, runner in CHAMPIONS:
        start = end_year - 1
        out.append((start, f"{start}-{str(end_year)[2:]}", champ, runner))
    return out
