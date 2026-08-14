"""Discovery must not create a second row for somebody we already have at this employer.

Reported from the live WebAI pair: two people four emails deep on *AI Forward Deployed Engineer*
(cancelled) were re-found for *AI Software Engineer* (live), and each new row was handed a fresh
cold email draft plus a text opening *"Alejandro here, I applied for the AI Software Engineer
role"* — written to somebody mid-conversation, as though none of it had happened.

The row is the visible half. The cost is the message.

`skip_known` already existed and did not cover this: it is opt-in (round two) and scoped to ONE
job, so a FIRST search on a second role at a company we already work had no exclusion at all.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.networking import store
from applypilot.repo import jobs as _jobs

DEAD = "https://jobs.example.com/webai/forward-deployed"
LIVE = "https://jobs.example.com/webai/software-engineer"
OTHER = "https://jobs.example.com/acme/staff"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    _jobs.insert_imported(DEAD, "AI Forward Deployed Engineer", "Webai", "Webai", DEAD, conn)
    _jobs.insert_imported(LIVE, "AI Software Engineer", "Webai", "Webai", LIVE, conn)
    _jobs.insert_imported(OTHER, "Staff Engineer", "Acme", "Acme", OTHER, conn)
    return conn


def _known(conn, **kw):
    row = {"job_url": DEAD, "full_name": "Marcus Godin", "email": "marcus.godin@webai.com",
           "company": "Webai", "outreach_status": "submitted", "sent_message_id": "gm1"}
    row.update(kw)
    return store.upsert_contact(row, conn)


def test_it_finds_people_we_hold_on_ANOTHER_role_at_this_employer(db):
    _known(db)
    got = store.known_at_company("Webai", exclude_job_url=LIVE, conn=db)
    assert [m["email"] for m in got] == ["marcus.godin@webai.com"]
    assert got[0]["emailed"] is True
    # Enough to NAME them in a refusal. A count is not actionable; "already on AI Forward
    # Deployed Engineer, emailed" is.
    assert got[0]["job_title"] == "AI Forward Deployed Engineer"


def test_the_job_being_searched_is_EXCLUDED(db):
    """Otherwise a re-run on the same role reports everyone already there as a duplicate and
    finds nobody — a working search that looks broken."""
    _known(db, job_url=LIVE)
    assert store.known_at_company("Webai", exclude_job_url=LIVE, conn=db) == []


def test_a_different_employer_is_never_returned(db):
    store.upsert_contact({"job_url": OTHER, "full_name": "Someone", "email": "s@acme.com",
                          "company": "Acme"}, db)
    assert store.known_at_company("Webai", exclude_job_url=LIVE, conn=db) == []


def test_an_empty_company_returns_nobody(db):
    """A blank employer must not match every contact whose company is also blank — that would
    exclude the entire database from every unresolved search."""
    _known(db)
    assert store.known_at_company("", exclude_job_url=LIVE, conn=db) == []


# ── the filter inside discovery ─────────────────────────────────────────────

def test_the_REAL_function_excludes_them(db, monkeypatch):
    """Drives `find_contacts_for_job` itself.

    The first version of this test rebuilt the filter inside the test and asserted on its own
    output — which proves the test agrees with itself and nothing about the shipped code
    (§Lessons 103). Only the provider is stubbed here; the exclusion under test is the real one.
    """
    from applypilot.networking import service
    _known(db)
    monkeypatch.setattr(service.derive, "resolve_employer", lambda j: ("Webai", "stored"))
    monkeypatch.setattr(service.derive, "derive_domain", lambda *a, **k: "webai.com")
    people = [
        {"key": "1", "full_name": "Marcus Godin", "email": "marcus.godin@webai.com",
         "company": "Webai", "title": "Engineer"},
        {"key": "2", "full_name": "New Person", "email": "new@webai.com",
         "company": "Webai", "title": "Engineer"}]
    monkeypatch.setattr(service.providers, "search_mix",
                        lambda *a, **k: {"peers": people, "recruiters": [], "note": ""})
    monkeypatch.setattr(service.providers, "company_known", lambda *a, **k: True)
    monkeypatch.setattr(service.providers, "available", lambda: True)
    monkeypatch.setattr(service.rank, "select_mix", lambda peers, recs, *a, **k: list(peers))

    got = service.find_contacts_for_job(
        {"url": LIVE, "title": "AI Software Engineer", "company": "Webai"},
        per_job=5, dry_run=True, draft=False)

    names = [m["full_name"] for m in got.get("known_elsewhere", [])]
    assert names == ["Marcus Godin"], f"the already-known contact was not excluded: {got}"
    assert "already on another role" in (got.get("note") or ""), got.get("note")


def test_the_exclusion_matches_on_EMAIL_not_on_contact_id(db):
    """`contact_id` hashes `job_url`, so it differs for exactly the rows this exists to catch.
    Matching on it would find nobody and the filter would be a no-op that looks implemented."""
    cid_dead = _known(db)
    cid_live = store.contact_id(LIVE, None, "Marcus Godin")
    assert cid_dead != cid_live, "the ids collide; this test proves nothing"
    got = store.known_at_company("Webai", exclude_job_url=LIVE, conn=db)
    assert got and got[0]["email"] == "marcus.godin@webai.com"


def test_it_reports_them_rather_than_dropping_them_silently(db):
    """§Lessons 15/91: a search that quietly kept 2 of 5 must not look like a company Apollo
    barely covers — and "already ours" is a DIFFERENT finding from "works elsewhere", with a
    different fix (move them, not correct the employer)."""
    import inspect

    from applypilot.networking import service
    src = inspect.getsource(service.find_contacts_for_job)
    assert "known_elsewhere" in src
    assert "result[\"known_elsewhere\"]" in src, "the skip never reaches the caller"
