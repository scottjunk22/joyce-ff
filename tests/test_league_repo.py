"""
Tests for the league rule-enforcement layer: auth, availability, trade, open
(bye-gated), lineup bye-flex, and fees.
"""

import pytest

from joyce_ff.league import auth, repo, schema

# NFL teams -> bye FF week (chosen to isolate the flex cases):
#   wk5: OT Blitz has 3 receivers on bye (MIA, SEA, SEA), 0 RBs
#   wk7: OT Blitz has 2 RBs on bye (ATL, BUF), 0 receivers
NFL_TEAMS = [("KC", "Kansas City", 6), ("BUF", "Buffalo", 7), ("DEN", "Denver", 7),
             ("CIN", "Cincinnati", 6), ("ATL", "Atlanta", 7), ("SF", "San Fran", 9),
             ("BUF2", "x", 7), ("MIA", "Miami", 5), ("SEA", "Seattle", 5),
             ("DAL", "Dallas", 11), ("CLE", "Cleveland", 9)]
PLAYERS = [
    ("p_bijan", "Bijan Robinson", "RB", "ATL"), ("p_kyren", "Kyren Williams", "RB", "SF"),
    ("p_cook", "James Cook", "RB", "BUF"), ("p_puka", "Puka Nacua", "WR", "MIA"),
    ("p_lamb", "CeeDee Lamb", "WR", "DAL"), ("p_mcbride", "Trey McBride", "TE", "SEA"),
    ("p_jsn", "Jaxon Smith-Njigba", "WR", "SEA"),
    # free agents (available):
    ("p_thielen", "Adam Thielen", "WR", "CLE"), ("p_warren", "Jaylen Warren", "RB", "DEN"),
]
OTB_ROSTER = [  # (kind, ref, unit_type, slot)
    ("TEAM_UNIT", "KC", "C", "C"), ("TEAM_UNIT", "BUF", "K", "K"),
    ("TEAM_UNIT", "DEN", "DEF/ST", "DEF/ST"), ("TEAM_UNIT", "CIN", "QB", "QB"),
    ("PLAYER", "p_bijan", None, "RB"), ("PLAYER", "p_kyren", None, "RB"),
    ("PLAYER", "p_cook", None, "RB"), ("PLAYER", "p_puka", None, "R"),
    ("PLAYER", "p_lamb", None, "R"), ("PLAYER", "p_mcbride", None, "R"),
    ("PLAYER", "p_jsn", None, "R"),
]


@pytest.fixture()
def db():
    c = schema.connect(":memory:")
    schema.init_db(c)
    sid = schema.seed_reference(c)
    for abbr, name, bye in NFL_TEAMS:
        c.execute("INSERT INTO nfl_teams(season_id,abbr,name,bye_ff_week) VALUES (?,?,?,?)",
                  (sid, abbr, name, bye))
    for gid, name, pos, team in PLAYERS:
        c.execute("INSERT INTO nfl_players(season_id,gsis_id,name,position,nfl_team_abbr) "
                  "VALUES (?,?,?,?,?)", (sid, gid, name, pos, team))
    otb = c.execute("SELECT id FROM teams WHERE name='OT Blitz'").fetchone()["id"]
    for kind, ref, unit, slot in OTB_ROSTER:
        c.execute("INSERT INTO roster_entries(season_id,team_id,asset_kind,asset_ref,"
                  "unit_type,roster_slot,acquired_ff_week,acquired_via,created_at) "
                  "VALUES (?,?,?,?,?,?,1,'DRAFT','t')", (sid, otb, kind, ref, unit, slot))
    c.commit()
    return c, sid, otb


# --- auth ---------------------------------------------------------------

def test_passcode_hash_roundtrip():
    h = auth.hash_passcode("hunter2")
    assert auth.verify_passcode("hunter2", h)
    assert not auth.verify_passcode("wrong", h)
    assert not auth.verify_passcode("hunter2", None)


def test_team_and_commissioner_passcodes(db):
    conn, sid, otb = db
    auth.set_team_passcode(conn, otb, "otblitz")
    assert auth.check_team_passcode(conn, otb, "otblitz")
    assert not auth.check_team_passcode(conn, otb, "nope")
    auth.set_admin_passcode(conn, "Steve", "commish")
    assert auth.is_commissioner(conn, "commish")
    assert not auth.is_commissioner(conn, "otblitz")


# --- availability (conference-scoped, separate pools) -------------------

def test_availability_excludes_owned_but_is_conference_scoped(db):
    conn, sid, otb = db
    blue = conn.execute("SELECT conference_id FROM teams WHERE id=?", (otb,)).fetchone()[0]
    red = conn.execute("SELECT id FROM conferences WHERE code='RED'").fetchone()["id"]
    avail_rb = {p["gsis_id"] for p in repo.available_players(conn, sid, blue, "RB")}
    assert "p_bijan" not in avail_rb           # owned by OT Blitz (Blue)
    assert "p_warren" in avail_rb              # free agent
    # separate pools: a Blue-owned player is still available in Red
    avail_rb_red = {p["gsis_id"] for p in repo.available_players(conn, sid, red, "RB")}
    assert "p_bijan" in avail_rb_red


# --- trade --------------------------------------------------------------

def test_trade_swaps_roster_and_logs_fee(db):
    conn, sid, otb = db
    repo.do_trade(conn, sid, otb, "RB", "p_cook", "p_warren", ff_week=3)
    refs = {e["asset_ref"] for e in repo.current_roster(conn, otb)}
    assert "p_warren" in refs and "p_cook" not in refs
    assert repo.fee_balance_cents(conn, otb)["owed_cents"] == 200


def test_roster_groups_by_slot_after_trade(db):
    conn, sid, otb = db
    repo.do_trade(conn, sid, otb, "RB", "p_cook", "p_warren", ff_week=3)   # new RB added
    slots = [e["roster_slot"] for e in repo.current_roster(conn, otb)]
    rank = {"C": 0, "K": 1, "DEF/ST": 2, "QB": 3, "RB": 4, "R": 5}
    # the freshly-acquired RB must sit with the other RBs, not dangling at the end
    assert slots == sorted(slots, key=lambda s: rank[s])


def test_traded_player_takes_replaced_players_slot(db):
    conn, sid, otb = db
    rbs = [e["asset_ref"] for e in repo.current_roster(conn, otb) if e["roster_slot"] == "RB"]
    assert rbs[0] == "p_bijan"                                   # first RB drafted
    repo.do_trade(conn, sid, otb, "RB", "p_bijan", "p_warren", ff_week=3)
    after = [e["asset_ref"] for e in repo.current_roster(conn, otb) if e["roster_slot"] == "RB"]
    assert after == ["p_warren", "p_kyren", "p_cook"]            # new RB took slot 1, not last


def test_trade_rejects_unowned_or_unavailable(db):
    conn, sid, otb = db
    with pytest.raises(repo.RuleError):
        repo.do_trade(conn, sid, otb, "RB", "p_warren", "p_bijan", 3)  # don't own warren
    with pytest.raises(repo.RuleError):
        repo.do_trade(conn, sid, otb, "RB", "p_cook", "p_kyren", 3)    # kyren already ours


# --- open (bye-gated) ---------------------------------------------------

def test_open_requires_the_player_to_be_on_bye(db):
    conn, sid, otb = db
    # Puka (MIA) is on bye week 5 -> allowed
    repo.do_open(conn, sid, otb, "R", "p_puka", "p_thielen", ff_week=5)
    assert repo.fee_balance_cents(conn, otb)["owed_cents"] == 200
    # Lamb (DAL, bye 11) is NOT on bye week 5 -> rejected
    with pytest.raises(repo.RuleError):
        repo.do_open(conn, sid, otb, "R", "p_lamb", "p_thielen", ff_week=5)


# --- lineup bye-flex ----------------------------------------------------

def _lineup(rb_refs, r_refs):
    base = [{"roster_slot": "C", "asset_ref": "KC"}, {"roster_slot": "K", "asset_ref": "BUF"},
            {"roster_slot": "DEF/ST", "asset_ref": "DEN"}, {"roster_slot": "QB", "asset_ref": "CIN"}]
    base += [{"roster_slot": "RB", "asset_ref": r} for r in rb_refs]
    base += [{"roster_slot": "R", "asset_ref": r} for r in r_refs]
    return base


def test_standard_2rb_3r_always_legal(db):
    conn, sid, otb = db
    repo.set_lineup(conn, sid, otb, 10,
                    _lineup(["p_bijan", "p_kyren"], ["p_puka", "p_lamb", "p_mcbride"]))
    n = conn.execute("SELECT COUNT(*) c FROM weekly_lineups WHERE team_id=? AND ff_week=10",
                     (otb,)).fetchone()["c"]
    assert n == 9


def test_third_rb_needs_two_receivers_on_bye(db):
    conn, sid, otb = db
    three_rb = _lineup(["p_bijan", "p_kyren", "p_cook"], ["p_puka", "p_lamb"])
    repo.set_lineup(conn, sid, otb, 5, three_rb)          # wk5: 3 receivers on bye -> ok
    with pytest.raises(repo.RuleError):
        repo.set_lineup(conn, sid, otb, 10, three_rb)     # wk10: none on bye -> rejected


def test_fourth_receiver_needs_two_rbs_on_bye(db):
    conn, sid, otb = db
    four_r = _lineup(["p_kyren"], ["p_puka", "p_lamb", "p_mcbride", "p_jsn"])
    repo.set_lineup(conn, sid, otb, 7, four_r)            # wk7: 2 RBs on bye -> ok
    with pytest.raises(repo.RuleError):
        repo.set_lineup(conn, sid, otb, 10, four_r)       # wk10: none -> rejected


def test_lineup_allows_open_rental(db):
    conn, sid, otb = db
    repo.do_open(conn, sid, otb, "R", "p_puka", "p_thielen", ff_week=5)   # Puka on bye wk5
    # start the rental (Thielen) in place of Puka
    lu = _lineup(["p_bijan", "p_kyren"], ["p_thielen", "p_lamb", "p_mcbride"])
    repo.set_lineup(conn, sid, otb, 5, lu)
    rental = conn.execute("SELECT is_rental FROM weekly_lineups WHERE team_id=? AND ff_week=5 "
                          "AND asset_ref='p_thielen'", (otb,)).fetchone()["is_rental"]
    assert rental == 1


def test_lineup_lock_blocks_changing_a_started_player(db):
    conn, sid, otb = db
    repo.set_lineup(conn, sid, otb, 10,
                    _lineup(["p_bijan", "p_kyren"], ["p_puka", "p_lamb", "p_mcbride"]))
    swap = _lineup(["p_cook", "p_kyren"], ["p_puka", "p_lamb", "p_mcbride"])   # bench bijan
    with pytest.raises(repo.RuleError):
        repo.set_lineup(conn, sid, otb, 10, swap, locked_refs={"p_bijan"})     # bijan locked
    # a change that doesn't touch any locked player is allowed
    repo.set_lineup(conn, sid, otb, 10, swap, locked_refs={"p_lamb"})          # lamb unchanged


def test_lineup_rejects_unowned_non_rental(db):
    conn, sid, otb = db
    lu = _lineup(["p_bijan", "p_kyren"], ["p_warren", "p_lamb", "p_mcbride"])  # warren not ours
    with pytest.raises(repo.RuleError):
        repo.set_lineup(conn, sid, otb, 10, lu)


# --- fees ---------------------------------------------------------------

def test_fee_balance_and_payment(db):
    conn, sid, otb = db
    repo.do_trade(conn, sid, otb, "RB", "p_cook", "p_warren", 3)      # $2
    repo.do_open(conn, sid, otb, "R", "p_puka", "p_thielen", 5)       # $2
    assert repo.fee_balance_cents(conn, otb)["balance_cents"] == 400
    repo.record_payment(conn, sid, otb, 300, note="partial")
    bal = repo.fee_balance_cents(conn, otb)
    assert bal["owed_cents"] == 400 and bal["paid_cents"] == 300 and bal["balance_cents"] == 100


def test_overpayment_leaves_a_credit(db):
    conn, sid, otb = db
    repo.do_trade(conn, sid, otb, "RB", "p_cook", "p_warren", 3)      # $2 owed
    repo.record_payment(conn, sid, otb, 500, note="overpay")          # pays $5
    assert repo.fee_balance_cents(conn, otb)["balance_cents"] == -300  # $3 credit
