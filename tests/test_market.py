"""No-network tests for the market-inefficiency scoring and tagging."""

import pandas as pd

from joyce_ff.projections import market


def test_standard_points_full_ppr_linear():
    df = pd.DataFrame([{
        "rushing_yards": 100, "receiving_yards": 50, "receptions": 6,
        "rushing_tds": 1, "receiving_tds": 0, "return_tds": 0,
    }])
    # 100*.1 + 50*.1 + 6*1 + 1*6 = 10 + 5 + 6 + 6 = 27
    assert market.standard_points(df).iloc[0] == 27.0


def test_tag_flags_ppr_volume_and_td_heavy():
    ppr_volume = {"td_pg": 0.2, "rec_pg": 6.0, "bust": 0.3}
    td_heavy = {"td_pg": 0.7, "rec_pg": 2.0, "bust": 0.25}
    assert "PPR-volume" in market._tag(ppr_volume)
    assert "TD-heavy" in market._tag(td_heavy)


def test_tag_steady_low_bust():
    assert "steady/low-bust" in market._tag({"td_pg": 0.4, "rec_pg": 3.0, "bust": 0.1})
