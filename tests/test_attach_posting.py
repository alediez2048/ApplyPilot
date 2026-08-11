"""A company card gains a job posting without becoming a different row — SHEET-1 C4.

The operator's framing was *"we can add the job later if any"*, which sounds like an insert and
is not one. `store.contact_id()` hashes `job_url`, and on a target card the anchor IS the
`job_url` — so writing the posting's URL into that column orphans every contact, every `touches`
ladder, every `sequences` row and every message on the card.

**Nothing would error.** The card would render with no people and no history, and the only clue
would be rows in `contacts` pointing at a `job_url` that no longer exists. That is CO-1's failure
with a new trigger, which is why the anchor is never in the request and the whole thing is an
UPDATE of three columns that were always there.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.networking import sheet_import, store
from applypilot.repo import jobs as _jobs
from applypilot.repo import spaces as _spaces

TSV = "\t".join
SHEET = "\n".join([
    TSV(["Company", "Name", "Email"]),
    TSV(["Ridgeline", "Dana Okafor", "dana@ridge.test"]),
    TSV(["Ridgeline", "Sam Iyer", ""]),
])
CARD = "target:sheets:ridgeline"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    _spaces.create_space("sheets", "Sheets", "sheet", conn=conn)
    sheet_import.import_sheet("sheets", SHEET, conn)
    return conn


def _people(conn):
    return [r["full_name"] for r in conn.execute(
        "SELECT full_name FROM contacts WHERE job_url = ? ORDER BY full_name", (CARD,))]


# ── the guarantee ───────────────────────────────────────────────────────────

def test_attaching_a_posting_does_not_change_the_anchor(db):
    """The single most important assertion in this file. If the url moves, every person on the
    card is orphaned and nothing raises."""
    before = _people(db)
    assert before == ["Dana Okafor", "Sam Iyer"]

    _jobs.attach_posting(CARD, title="Solutions Engineer",
                         application_url="https://ridge.test/jobs/42", conn=db)

    assert db.execute("SELECT COUNT(*) FROM jobs WHERE url = ?", (CARD,)).fetchone()[0] == 1
    assert _people(db) == before, "the anchor moved and the card lost its people"


def test_the_ladders_and_history_survive(db):
    """Contacts are only half of it — `touches` and `sequences` are keyed by contact_id, which
    is itself derived from the anchor."""
    cid = db.execute("SELECT id FROM contacts WHERE full_name='Dana Okafor'").fetchone()["id"]
    db.execute("UPDATE contacts SET sent_message_id='m1', outreach_status='sent' WHERE id=?", (cid,))
    db.commit()

    _jobs.attach_posting(CARD, title="Solutions Engineer", conn=db)

    r = db.execute("SELECT sent_message_id, outreach_status, job_url FROM contacts WHERE id=?",
                   (cid,)).fetchone()
    assert r["sent_message_id"] == "m1" and r["outreach_status"] == "sent"
    assert r["job_url"] == CARD


def test_no_contact_is_left_pointing_at_a_row_that_does_not_exist(db):
    """The shape the failure would take: rows in `contacts` whose `job_url` matches no job."""
    _jobs.attach_posting(CARD, title="Solutions Engineer", conn=db)
    orphans = db.execute(
        "SELECT COUNT(*) FROM contacts c LEFT JOIN jobs j ON c.job_url = j.url "
        "WHERE j.url IS NULL").fetchone()[0]
    assert orphans == 0


# ── what it writes ──────────────────────────────────────────────────────────

def test_it_fills_the_columns_that_were_always_there(db):
    """No schema change: `title`, `application_url` and `full_description` exist on every jobs
    row and simply sat empty on a target."""
    out = _jobs.attach_posting(CARD, title="Solutions Engineer",
                               application_url="https://ridge.test/jobs/42",
                               description="Build integrations for freight customers.", conn=db)
    assert set(out["changed"]) >= {"title", "application_url", "full_description"}
    r = db.execute("SELECT title, application_url, full_description FROM jobs WHERE url=?",
                   (CARD,)).fetchone()
    assert r["title"] == "Solutions Engineer"
    assert r["application_url"] == "https://ridge.test/jobs/42"
    assert "freight customers" in r["full_description"]


def test_it_only_fills_what_it_is_given(db):
    """A caller that knows the link but not the description must not blank a description the
    operator typed."""
    _jobs.attach_posting(CARD, description="Typed by hand.", conn=db)
    _jobs.attach_posting(CARD, application_url="https://ridge.test/jobs/42", conn=db)
    r = db.execute("SELECT full_description, application_url FROM jobs WHERE url=?",
                   (CARD,)).fetchone()
    assert r["full_description"] == "Typed by hand."
    assert r["application_url"] == "https://ridge.test/jobs/42"


def test_the_company_name_is_never_overwritten(db):
    """`company` identifies the card and is hashed into its anchor. A posting's own idea of the
    employer must not reach it — that is how "Ouryahoo" and "Docs" got stored in the first
    place, and here the operator STATED the name."""
    _jobs.attach_posting(CARD, title="Solutions Engineer", conn=db)
    assert db.execute("SELECT company FROM jobs WHERE url=?", (CARD,)).fetchone()["company"] \
        == "Ridgeline"


def test_a_description_clears_the_scrape_error(db):
    """§Lessons 44: a row carrying `detail_error` with a real description now is a state that
    renders like a failure while being fine."""
    db.execute("UPDATE jobs SET detail_error='no data extracted' WHERE url=?", (CARD,))
    db.commit()
    _jobs.attach_posting(CARD, description="Real text.", conn=db)
    r = db.execute("SELECT detail_error, detail_scraped_at FROM jobs WHERE url=?", (CARD,)).fetchone()
    assert not r["detail_error"]
    assert r["detail_scraped_at"]


def test_attaching_nothing_changes_nothing(db):
    assert _jobs.attach_posting(CARD, conn=db)["changed"] == []


def test_an_unknown_card_raises(db):
    with pytest.raises(ValueError):
        _jobs.attach_posting("target:sheets:nope", title="X", conn=db)


# ── the endpoint ────────────────────────────────────────────────────────────

def test_the_endpoint_attaches_and_reports(db):
    out = wd._attach_posting({"url": CARD, "title": "Solutions Engineer"})
    assert out["ok"] is True and "title" in out["changed"]
    assert _people(db) == ["Dana Okafor", "Sam Iyer"]


def test_the_endpoint_refuses_an_empty_attach(db):
    out = wd._attach_posting({"url": CARD})
    assert out["ok"] is False and "Nothing to attach" in out["message"]


def test_the_endpoint_cannot_be_asked_to_rekey_a_card(db):
    """The anchor is not a parameter. A request naming a new url must not be able to move the
    row — the whole guarantee would otherwise depend on the browser being well behaved."""
    import inspect
    src = inspect.getsource(_jobs.attach_posting)
    assert "SET url" not in src and "url = ?," not in src
    wd._attach_posting({"url": CARD, "title": "X",
                        "new_url": "https://ridge.test/jobs/42",
                        "anchor": "target:sheets:something-else"})
    assert db.execute("SELECT COUNT(*) FROM jobs WHERE url=?", (CARD,)).fetchone()[0] == 1
    assert _people(db) == ["Dana Okafor", "Sam Iyer"]
