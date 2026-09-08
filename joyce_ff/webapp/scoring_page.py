"""
Render the Point System page FROM the scoring engine's own constants.

The rulebook lives in joyce_ff/scoring/rules.py and is what actually scores every
game. Transcribing it into a page would create a second copy that quietly drifts,
so the page is generated from the same values instead: change a rule and the page
changes with it.
"""

from __future__ import annotations

from ..scoring import rules as R


def _rows(pairs) -> str:
    return "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in pairs)


def _card(title: str, pairs, note: str | None = None) -> str:
    return (f'<div class="card"><h2>{title}</h2><table>{_rows(pairs)}</table>'
            + (f'<div class="note">{note}</div>' if note else "")
            + "</div>")


def _pt(n: int) -> str:
    return f"{n} pt" if n == 1 else f"{n} pts"


def _ladder(tiers, extension, unit="yards"):
    """A threshold ladder as readable bands: 75-99, 100-124, ... plus the
    repeating rule past the top tier."""
    out = [(f"under {tiers[0][0]} {unit}", "0")]
    for i, (thresh, pts) in enumerate(tiers):
        if i + 1 < len(tiers):
            out.append((f"{thresh}–{tiers[i + 1][0] - 1} {unit}", _pt(pts)))
        else:
            out.append((f"{thresh}+ {unit}", _pt(pts)))
    if extension:
        out.append((f"every {extension['step_yards']} {unit} beyond {extension['step_from']}",
                    f"+{_pt(extension['step_points'])}"))
    return out


def _allowed(unit, tiers):
    """Defensive 'allowed' tiers are capped maximums, so the second one is a
    BAND, not another 'or fewer' — 249 yards pays 3, but 150 yards pays 5.
    Writing both as 'or fewer' would read as though the lower tier applied too."""
    out, prev = [], None
    for cap, pts in tiers:
        label = (f"Allowing {cap} {unit} or fewer" if prev is None
                 else f"Allowing {prev + 1}–{cap} {unit}")
        out.append((label, _pt(pts)))
        prev = cap
    return out


def build_cards() -> str:
    fg = R.FIELD_GOAL_TIERS
    fg_rows = []
    for i, (d, p) in enumerate(fg):
        lo = 1 if d == 0 else d
        hi = fg[i + 1][0] - 1 if i + 1 < len(fg) else None
        fg_rows.append((f"{lo}–{hi} yards" if hi else f"{lo}+ yards", _pt(p)))

    cards = [
        _card("Touchdowns &amp; conversions", [
            ("Touchdown (rush, catch or return)", _pt(R.TD_ANY)),
            ("Throwing a touchdown — to the QB slot", _pt(R.TD_PASS_TO_PASSER)),
            ("Two-point conversion", _pt(R.TWO_POINT_CONVERSION)),
            ("Extra-point pass", _pt(R.EXTRA_POINT_PASS)),
            ("Safety", _pt(R.SAFETY)),
        ]),
        _card("Rushing yards", _ladder(R.RUSHING_YARD_TIERS, R.RUSHING_YARD_EXTENSION),
              "Per game. Rushing and receiving yards are counted separately, "
              "not added together."),
        _card("Receiving yards", _ladder(R.RECEIVING_YARD_TIERS, R.RECEIVING_YARD_EXTENSION)),
        _card("Receptions", _ladder(R.RECEPTION_TIERS, R.RECEPTION_EXTENSION, unit="catches"),
              "On top of receiving yards."),
        _card("Passing yards — QB slot",
              _ladder(R.PASSING_YARD_TIERS, R.PASSING_YARD_EXTENSION),
              "The whole team's passing, whoever throws it."),
        _card("Kicker", fg_rows + [("Extra point", _pt(R.EXTRA_POINT))]),
        _card("Coach", [("Each NFL win", _pt(R.COACH_WIN))],
              "A 14-win team's coach is worth 42 points across the season."),
        _card("Defense / Special teams", [
            ("Defensive or special-teams touchdown", _pt(R.DEF_TD)),
            ("Interception", _pt(R.DEF_INTERCEPTION)),
            ("Fumble recovery", _pt(R.DEF_FUMBLE_RECOVERY)),
            ("Sack", _pt(R.DEF_SACK)),
            ("Safety", _pt(R.DEF_SAFETY)),
        ] + _allowed("points", R.DEF_POINTS_ALLOWED_TIERS)
          + _allowed("yards", R.DEF_YARDS_ALLOWED_TIERS),
              "Points and yards allowed are scored separately, so a dominant game can earn both. "
              "Giving up a lot simply earns nothing — never a negative."),
    ]
    return "".join(cards)
