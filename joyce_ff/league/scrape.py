"""
Legacy-site cross-check.

Fetches the hand-maintained Jimdo site (read-only, one polite request), stores
an immutable snapshot, and records each team's POSTED weekly total into
team_week_scores.posted_points so run_week's reconcile can compare our engine
against the commissioner's numbers.

Team labels on the site are unreliable (documented), so we match by normalized
name and only store what we can match — never guessing.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def scrape_and_store(conn, season_id: int, ff_week: int, html: str | None = None) -> dict:
    from ..data_sources import league_site as ls

    if html is None:
        html = ls.fetch_home()
    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    conn.execute("INSERT INTO scrape_snapshots(fetched_at_utc,url,content_sha256,raw_text) "
                 "VALUES (?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), ls.HOME_URL, sha, html))

    by_name = {_norm(r["name"]): r["id"] for r in conn.execute(
        "SELECT id, name FROM teams WHERE season_id=?", (season_id,))}
    matched, unmatched = 0, []
    for lu in ls.parse_lineups(html):
        if lu.posted_total is None:
            continue
        tid = by_name.get(_norm(lu.team))
        if tid is None:
            unmatched.append(lu.team)
            continue
        conn.execute(
            "INSERT INTO team_week_scores(season_id,team_id,ff_week,posted_points) "
            "VALUES (?,?,?,?) ON CONFLICT(season_id,team_id,ff_week) DO UPDATE SET "
            "posted_points=excluded.posted_points", (season_id, tid, ff_week, lu.posted_total))
        matched += 1
    conn.commit()
    return {"matched": matched, "unmatched": unmatched, "sha": sha[:12]}
