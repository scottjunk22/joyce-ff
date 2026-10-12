"""Announcements: the commissioner's notes on the main page (Scott, 2026-10-11)."""

import pytest

from joyce_ff.league import announcements, schema
from test_webapp import client  # noqa: F401  (the web fixture)


@pytest.fixture()
def db():
    c = schema.connect(":memory:")
    schema.init_db(c)
    return c


def test_newest_first_until_one_is_removed(db):
    first = announcements.post(db, "Dues", "Entry fees are due at the draft.", "Steve")
    second = announcements.post(db, None, "  Second note,\r\nwith a line break.  \n\n\n\nAnd a gap.  ", "Scott")
    shown = announcements.active(db)
    assert [a["id"] for a in shown] == [second, first]
    assert shown[0] == {"id": second, "title": None, "by": "Scott", "at": shown[0]["at"], "edited": False,
                        "body": "Second note,\nwith a line break.\n\nAnd a gap."}
    announcements.remove(db, first, "Steve")
    assert [a["id"] for a in announcements.active(db)] == [second]
    # hidden, not deleted: what was said can still be looked up
    kept = db.execute("SELECT body, removed_by, removed_at FROM announcements WHERE id=?", (first,)).fetchone()
    assert kept["body"].startswith("Entry fees") and kept["removed_by"] == "Steve" and kept["removed_at"]


def test_an_edit_changes_the_words_not_who_or_when(db):
    a = announcements.post(db, "Old", "Old words.", "Steve")
    before = announcements.active(db)[0]
    announcements.edit(db, a, "", "New words.")
    after = announcements.active(db)[0]
    assert (after["title"], after["body"], after["edited"]) == (None, "New words.", True)
    assert (after["by"], after["at"]) == (before["by"], before["at"])


def test_what_it_refuses(db):
    with pytest.raises(announcements.AnnouncementError, match="empty"):
        announcements.post(db, "Headline only", "   \n  ", "Steve")
    with pytest.raises(announcements.AnnouncementError, match="headline"):
        announcements.post(db, "x" * 81, "fine", "Steve")
    with pytest.raises(announcements.AnnouncementError, match="message"):
        announcements.post(db, None, "x" * 1501, "Steve")
    a = announcements.post(db, None, "one", "Steve")
    announcements.remove(db, a)
    with pytest.raises(announcements.AnnouncementError, match="isn't there any more"):
        announcements.edit(db, a, None, "two")
    with pytest.raises(announcements.AnnouncementError, match="isn't there any more"):
        announcements.remove(db, a)
    assert announcements.active(db) == []


def test_only_a_commissioner_can_post_and_everyone_can_read(client):  # noqa: F811
    assert client.get("/api/state").get_json()["announcements"] == []
    body = {"title": "Week 4", "body": "<b>Lineups</b> lock at kickoff.\nNo exceptions."}
    for bad in ({}, {"passcode": "otblitz"}, {"passcode": "wrong"}):          # a team PIN isn't enough
        assert client.post("/api/admin/announcement", json={**body, **bad}).status_code == 403
    r = client.post("/api/admin/announcement", json={**body, "passcode": "commish"})
    assert r.status_code == 200, r.get_json()
    (a,) = client.get("/api/state").get_json()["announcements"]               # no passcode needed to read
    assert (a["title"], a["by"]) == ("Week 4", "Steve")
    assert a["body"] == "<b>Lineups</b> lock at kickoff.\nNo exceptions."      # stored as typed; the page escapes it
    r = client.post(f"/api/admin/announcement/{a['id']}", json={"title": "", "body": "Changed.", "passcode": "commish"})
    assert r.get_json()["announcements"][0]["body"] == "Changed."
    assert client.post(f"/api/admin/announcement/{a['id']}", json={"body": "", "passcode": "commish"}).status_code == 400
    assert client.post(f"/api/admin/announcement/{a['id']}/delete", json={"passcode": "otblitz"}).status_code == 403
    r = client.post(f"/api/admin/announcement/{a['id']}/delete", json={"passcode": "commish"})
    assert r.get_json() == {"ok": True, "announcements": []}
    assert client.get("/api/state").get_json()["announcements"] == []


def test_a_database_made_before_announcements_gets_the_table(tmp_path):
    c = schema.connect(tmp_path / "old.sqlite")
    schema.init_db(c)
    c.execute("DROP TABLE announcements")                  # as the live database is today
    c.commit()
    schema.migrate(c)
    assert announcements.active(c) == []
