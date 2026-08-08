"""
No-network tests for the board JSON serialization and the web page rendering
(NaN safety and HTML-injection escaping). The heavy build_all() path needs
nflverse data and is exercised by `manage.py board` / integration, not here.
"""

import json
import math

from joyce_ff.projections import board as B
from joyce_ff.web import server as S


def test_num_is_json_safe():
    assert B._num(float("nan")) is None
    assert B._num(float("inf")) is None
    assert B._num(None) is None
    assert B._num("x") is None
    assert B._num(3.14159) == 3.14


def test_render_replaces_placeholder_and_escapes_lt():
    data = {
        "generated_at": "2026-08-08T00:00:00+00:00",
        "seasons": [2023, 2024, 2025], "draft_season": 2026,
        "league": {"teams": 22, "rostered_rb": 66, "rostered_r": 88,
                   "started_rb": 44, "started_r": 66},
        "replacement": {"RB": 1.5, "R": 2.48},
        "players": [{"rank": 1, "slot": "RB", "tier": 1,
                     "name": "<script>alert(1)</script>", "team": "SF",
                     "position": "RB", "proj": 7.0, "vor": 5.0, "floor": 2.0,
                     "ceil": 12.0, "bust": 12, "games25": 17,
                     "low_sample": False, "no_history": False}],
        "units": {"QB": [], "K": [], "DEF/ST": [], "C": []},
    }
    page = S._render(data)
    assert "__BOARD_DATA__" not in page
    # the raw '<' from the injected name must be escaped so it can't open a tag
    assert "<script>alert(1)</script>" not in page
    assert "\\u003cscript" in page


def test_render_payload_is_valid_json_roundtrip():
    data = {"generated_at": "t", "seasons": [2025], "draft_season": 2026,
            "league": {}, "replacement": {}, "players": [], "units": {}}
    page = S._render(data)
    start = page.index("const DATA = ") + len("const DATA = ")
    end = page.index(";\n", start)
    payload = page[start:end].replace("\\u003c", "<")
    assert json.loads(payload)["draft_season"] == 2026
