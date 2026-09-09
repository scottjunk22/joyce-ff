"""The /dark practice universe: a separate database that still shares the
things that belong to the league rather than to a season."""

from __future__ import annotations


def test_practice_universe_inherits_league_history(tmp_path):
    """The champions band is league history, not season data — a practice run
    shows the same past winners the real site does (it was blank before)."""
    from joyce_ff.league import schema
    from joyce_ff.webapp import create_app

    real = tmp_path / "league.sqlite"
    conn = schema.connect(str(real))
    schema.init_db(conn)
    schema.migrate(conn)
    conn.execute("INSERT INTO champions(year,label,team,runner_up) VALUES (?,?,?,?)",
                 (2025, "2025-26", "Ribears", "Pike"))
    conn.commit()
    conn.close()

    app = create_app(str(real))
    with app.test_client() as c:
        assert b"Ribears" in c.get("/dark/history").data
