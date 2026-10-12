"""
Announcements: the commissioner's notes to the league, shown on the main page
between the scoreboard and the standings (Scott, 2026-10-11).

Plain text, an optional headline, newest first. They stay up until a
commissioner removes one — nothing expires by itself. They belong to the
league, not to a season: a note posted in January is still there in August
unless someone takes it down.

A removed announcement is hidden, not deleted (removed_at / removed_by), so
what was said and when can still be looked up. An edit does overwrite the
text; updated_at records that it happened.

The text is stored exactly as typed and is NOT trusted: the page escapes it
before showing it. Nothing here turns a web address into a link.
"""

from __future__ import annotations

from datetime import datetime, timezone

TITLE_MAX = 80
BODY_MAX = 1500


class AnnouncementError(ValueError):
    """A post or edit the commissioner should be told about in plain words."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(title, body) -> tuple[str | None, str]:
    title = " ".join(str(title or "").split())            # one line, single spaces
    # keep his line breaks, but not a wall of blank ones or trailing spaces
    lines = [ln.rstrip() for ln in str(body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    body = "\n".join(out).strip()
    if not body:
        raise AnnouncementError("write a message first — the announcement is empty")
    if len(title) > TITLE_MAX:
        raise AnnouncementError(f"the headline is {len(title)} characters — keep it to {TITLE_MAX}")
    if len(body) > BODY_MAX:
        raise AnnouncementError(f"the message is {len(body)} characters — keep it to {BODY_MAX}")
    return (title or None), body


def active(conn) -> list[dict]:
    """Everything showing, newest first."""
    return [{"id": r["id"], "title": r["title"], "body": r["body"], "by": r["posted_by"],
             "at": r["created_at"], "edited": bool(r["updated_at"])}
            for r in conn.execute(
                "SELECT id, title, body, posted_by, created_at, updated_at FROM announcements "
                "WHERE removed_at IS NULL ORDER BY created_at DESC, id DESC")]


def post(conn, title, body, by=None) -> int:
    title, body = _clean(title, body)
    cur = conn.execute("INSERT INTO announcements(title, body, posted_by, created_at) VALUES (?,?,?,?)",
                       (title, body, by, _now()))
    conn.commit()
    return int(cur.lastrowid)


def _showing(conn, ann_id):
    row = conn.execute("SELECT id FROM announcements WHERE id=? AND removed_at IS NULL",
                       (ann_id,)).fetchone()
    if not row:
        raise AnnouncementError("that announcement isn't there any more — it may already have been removed")


def edit(conn, ann_id, title, body) -> None:
    """Change the words. Who posted it and when stay as they were."""
    title, body = _clean(title, body)
    _showing(conn, ann_id)
    conn.execute("UPDATE announcements SET title=?, body=?, updated_at=? WHERE id=?",
                 (title, body, _now(), ann_id))
    conn.commit()


def remove(conn, ann_id, by=None) -> None:
    _showing(conn, ann_id)
    conn.execute("UPDATE announcements SET removed_at=?, removed_by=? WHERE id=?",
                 (_now(), by, ann_id))
    conn.commit()
