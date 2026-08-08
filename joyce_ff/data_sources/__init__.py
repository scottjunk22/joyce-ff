"""Data sources: nflverse (NFL stats) and the league site (league state).

Kept separate from the scoring engine so the engine stays pure and
dependency-free. Anything that touches pandas / the network lives here.
"""
