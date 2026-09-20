"""
Importing FantasyPros' consensus rankings (Scott, 2026-09-19).

The file is somebody else's export and the names in it are somebody else's
spelling, so the rules under test are the careful ones: read only what the
header promises, map their positions onto OUR slots, and when a name is
ambiguous or absent, say so instead of guessing.
"""

import pytest

from joyce_ff.projections import ecr

CSV = '''"RK","PLAYER NAME",TEAM,"POS","SOS SEASON","SOS PLAYOFFS","ECR VS. ADP"
"1","Jahmyr Gibbs",DET,"RB1","5 out of 5 stars","4 out of 5 stars","0"
"2","James Cook III",BUF,"RB2","2 out of 5 stars","4 out of 5 stars","+3"
""
"3","Ja'Marr Chase",CIN,"WR1","4 out of 5 stars","3 out of 5 stars","-8"
"4","Brock Bowers",LV,"TE1","3 out of 5 stars","5 out of 5 stars","-24"
"5","Josh Allen",BUF,"QB1","4 out of 5 stars","4 out of 5 stars","-7"
"6","Puka Nacua",LAR,"WR2","0 out of 5 stars","2 out of 5 stars","-10"
"7","Trevor Lawrence",JAC,"QB7","5 out of 5 stars","5 out of 5 stars","+28"
"8","Mitchell Trubisky",BUF,"QB30","1 out of 5 stars","1 out of 5 stars","-"
"9","Houston Texans",HOU,"DST1","2 out of 5 stars","1 out of 5 stars","-18"
"10","Brandon Aubrey",DAL,"K1","1 out of 5 stars","2 out of 5 stars","-57"
"11","Tyreek Hill",FA,"WR9","-","-","-175"
'''


@pytest.fixture()
def rows(tmp_path):
    p = tmp_path / "ranks.csv"
    p.write_text(CSV, encoding="utf-8")
    return ecr.parse_csv(p)


def test_it_reads_their_export_including_the_blank_spacer_rows(rows):
    assert len(rows) == 11
    assert rows[0] == {"rank": 1, "name": "Jahmyr Gibbs", "team": "DET",
                       "pos": "RB", "pos_rank": 1}


def test_their_team_spellings_become_the_schedule_s(rows):
    """We store the NFL schedule's codes — the Rams are LA there, LAR here."""
    assert [r["team"] for r in rows if r["name"] == "Puka Nacua"] == ["LA"]
    assert [r["team"] for r in rows if r["name"] == "Trevor Lawrence"] == ["JAX"]
    assert [r["team"] for r in rows if r["name"] == "Tyreek Hill"] == [None]


def test_a_file_that_isnt_their_export_is_an_error_not_an_empty_column(tmp_path):
    p = tmp_path / "wrong.csv"
    p.write_text("name,points\nBijan,12\n", encoding="utf-8")
    with pytest.raises(ecr.EcrError):
        ecr.parse_csv(p)


POOL = [
    {"player_id": "p_gibbs", "name": "Jahmyr Gibbs", "team": "DET"},
    {"player_id": "p_cook", "name": "James Cook", "team": "BUF"},
    {"player_id": "p_chase", "name": "Ja'Marr Chase", "team": "CIN"},
    {"player_id": "p_bowers", "name": "Brock Bowers", "team": "LV"},
    {"player_id": "p_nacua", "name": "Puka Nacua", "team": "LA"},
]


def test_players_map_onto_our_slots_and_suffixes_dont_break_a_match(rows):
    m = ecr.match(rows, POOL)
    assert m["players"]["p_cook"]["ecr"] == 2            # "James Cook III"
    assert m["players"]["p_nacua"]["ecr"] == 6           # matched through LAR -> LA
    assert set(m["players"]) == {"p_gibbs", "p_cook", "p_chase", "p_bowers", "p_nacua"}


def test_a_unit_takes_its_team_s_best_ranked_player(rows):
    """You own the QB room, not the quarterback — so it's ranked by whichever of
    that team's QBs the consensus rates highest."""
    m = ecr.match(rows, POOL)
    assert m["units"][("QB", "BUF")]["ecr"] == 5         # Allen, not the backup at 8
    assert m["units"][("QB", "JAX")]["ecr"] == 7
    assert m["units"][("K", "DAL")]["ecr"] == 10
    assert m["units"][("DEF/ST", "HOU")]["ecr"] == 9
    assert ("C", "DET") not in m["units"]                # nothing ranks coaches


def test_a_player_nobody_rosters_is_reported_not_guessed(rows):
    m = ecr.match(rows, POOL)
    assert m["unmatched"] == ["Tyreek Hill"]             # FA, on no 2026 roster
    assert (m["matched"], m["total"]) == (10, 11)


def test_two_players_of_the_same_name_are_left_unmatched(rows):
    """Better a blank cell than one man's ranking on another man's row."""
    twins = POOL + [{"player_id": "p_gibbs2", "name": "Jahmyr Gibbs", "team": "DET"}]
    m = ecr.match([r for r in rows if r["name"] == "Jahmyr Gibbs"], twins)
    assert m["players"] == {}                            # same name, same team
    assert m["unmatched"] == ["Jahmyr Gibbs"]


def test_the_team_breaks_a_tie_when_it_can(rows):
    twins = POOL + [{"player_id": "p_gibbs2", "name": "Jahmyr Gibbs", "team": "KC"}]
    m = ecr.match([{"rank": 1, "name": "Jahmyr Gibbs", "team": "DET",
                    "pos": "RB", "pos_rank": 1}], twins)
    assert set(m["players"]) == {"p_gibbs"}


def test_a_known_nickname_is_matched_only_because_it_was_written_down():
    """NAME_ALIAS is hand-checked against the roster, like titles.ALIASES."""
    pool = [{"player_id": "p_brown", "name": "Marquise Brown", "team": "PHI"}]
    m = ecr.match([{"rank": 12, "name": "Hollywood Brown", "team": "PHI",
                    "pos": "WR", "pos_rank": 5}], pool)
    assert m["players"]["p_brown"]["ecr"] == 12
