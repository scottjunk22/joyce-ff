"""
Draft valuation: distributional projections integrated against the threshold
ladder, replacement level at 22-team settings, and value over replacement.

The engine stays pure; this package pulls nflverse history, scores every real
game with the engine, and reduces those per-game score distributions into
draft-time numbers. We never average yardage and then score it — we score each
game and average the POINTS, which is what makes threshold scoring behave.
"""
