"""ARCH-4: /api/status must not get chattier, and must not scale per contact.

The dashboard re-renders every 2.5 seconds, so anything on this path is effectively in a
hot loop. Measured before touching anything: **313 SQL statements per request**, of which
199 were `CREATE TABLE IF NOT EXISTS` / `PRAGMA table_info` re-run on every single call —
`init_connections` alone fired 108 because it was invoked once per contact.

Idempotent-at-the-SQL-level is not the same as free. Nothing failed, nothing was slow
enough to notice, and nothing would ever have reported it. Only counting did.

The N+1 guard below is the one that matters long-term: it fails if someone adds a
per-contact lookup, which is the specific regression ARCH-4 warns about.
"""

from __future__ import annotations

import re

import pytest

import applypilot.database as database
from applypilot.networking import connections, store, touches

# Headroom over the measured figure, not a target to grow into. If a change needs more
# than this, it is doing per-row work on the request path and should be batched instead.
MAX_STATEMENTS = 80


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    connections.init_connections(conn)
    return conn


def _seed(conn, jobs: int, contacts_per_job: int) -> None:
    for j in range(jobs):
        url = f"http://j/{j}"
        # `strategy` matters: /api/status filters on it (_URL_QUEUE_SQL). Without it the
        # payload comes back EMPTY and every assertion below measures nothing — which is
        # exactly how the first version of this file passed while proving nothing.
        conn.execute("INSERT INTO jobs (url, title, site, strategy, tailored_resume_path, "
                     "discovered_at) VALUES (?,?,?,?,?,?)",
                     (url, f"Job {j}", "Greenhouse", "dashboard_upload", "/tmp/r.pdf",
                      f"2026-07-2{j % 9}T10:00:00+00:00"))
        for c in range(contacts_per_job):
            store.upsert_contact({"job_url": url, "full_name": f"Person {j}-{c}",
                                  "email": f"p{j}{c}@x.com", "sent_message_id": "gid",
                                  "submitted_at": "2026-07-20T10:00:00+00:00"}, conn)
    for n in range(200):        # a connections table big enough that a full scan would hurt
        conn.execute("INSERT INTO connections (name_norm, full_name, company, company_norm) "
                     "VALUES (?,?,?,?)", (f"person {n}", f"Person {n}", "Acme", "acme"))
    conn.commit()


def _count_statements(conn, fn) -> int:
    """Steady-state cost: run once to let one-time schema setup happen, then measure."""
    fn()
    n = 0

    def trace(_stmt):
        nonlocal n
        n += 1
    conn.set_trace_callback(trace)
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return n


def test_the_fixture_actually_produces_a_payload(db):
    """Guard the guard. Every assertion here is meaningless against an empty payload."""
    from applypilot import web_dashboard as wd
    _seed(db, jobs=8, contacts_per_job=4)
    payload = wd._status_payload()
    assert len(payload["jobs"]) == 8
    assert sum(len(j["contacts"]) for j in payload["jobs"]) == 32


def test_status_payload_stays_within_the_query_budget(db):
    from applypilot import web_dashboard as wd
    _seed(db, jobs=8, contacts_per_job=4)
    assert len(wd._status_payload()["jobs"]) == 8, "empty payload — the budget means nothing"
    n = _count_statements(db, wd._status_payload)
    assert n <= MAX_STATEMENTS, (
        f"/api/status now runs {n} statements (budget {MAX_STATEMENTS}). "
        "This path runs every 2.5s — batch the new query instead of raising the budget."
    )


def test_query_count_does_not_grow_with_contacts(db):
    """The N+1 guard. Contacts per job go up 4x; statement count must not follow.

    Both ladder state and connection matching are bulk-loaded per job precisely so this
    holds. A `for contact in contacts: some_lookup(contact)` added anywhere on this path
    fails here.
    """
    from applypilot import web_dashboard as wd
    _seed(db, jobs=4, contacts_per_job=2)
    assert sum(len(j["contacts"]) for j in wd._status_payload()["jobs"]) == 8
    few = _count_statements(db, wd._status_payload)

    for j in range(4):                      # 2 -> 8 contacts per job
        for c in range(2, 8):
            store.upsert_contact({"job_url": f"http://j/{j}", "full_name": f"Person {j}-{c}",
                                  "email": f"p{j}{c}@x.com", "sent_message_id": "gid",
                                  "submitted_at": "2026-07-20T10:00:00+00:00"}, db)
    db.commit()
    assert sum(len(j["contacts"]) for j in wd._status_payload()["jobs"]) == 32
    many = _count_statements(db, wd._status_payload)

    assert many <= few + 2, (
        f"{few} statements for 8 contacts, {many} for 32 — the query count is scaling "
        "with contacts, which is the N+1 this ticket exists to prevent."
    )


def test_status_does_not_ask_gmail_who_we_are_once_per_job(db, monkeypatch):
    """SQL is not the only thing this path can do too often.

    CRM-4a introduced `connected_email()` into the render path, once per job. It is an HTTP
    round-trip to Gmail (~0.12s), so with 15 jobs `/api/status` measured **2.4s** against a
    2.5s refresh — the dashboard was refreshing back-to-back and spending nearly all of it
    asking Gmail the same unchanging question. The statement budget above could not see it,
    because none of it was SQL.
    """
    from applypilot import web_dashboard as wd
    from applypilot.networking import gmail_oauth

    calls = {"n": 0}

    class _Profile:
        def execute(self):
            calls["n"] += 1
            return {"emailAddress": "me@example.com"}

    class _Users:
        def getProfile(self, userId=None):
            return _Profile()

    class _Svc:
        def users(self):
            return _Users()

    gmail_oauth._EMAIL_CACHE.clear()
    monkeypatch.setattr(gmail_oauth, "_load_creds", lambda: object())
    monkeypatch.setattr(gmail_oauth, "_libs", lambda: (None, None, None,
                                                       lambda *a, **k: _Svc()))

    _seed(db, jobs=8, contacts_per_job=4)
    wd._status_payload()
    wd._status_payload()
    assert calls["n"] <= 1, (
        f"{calls['n']} Gmail profile fetches for 8 jobs across 2 renders — this is a network "
        "call on a 2.5s loop; cache it rather than repeating it per job.")


def test_reconnecting_a_different_account_is_not_served_from_the_cache(monkeypatch, tmp_path):
    """A cache that outlives the thing it caches is how you email as the wrong person. Keyed on
    the token file's mtime, so writing a new token invalidates it."""
    from applypilot.networking import gmail_oauth

    token = tmp_path / "gmail_token.json"
    token.write_text("{}", encoding="utf-8")
    who = {"addr": "first@example.com"}

    class _Svc:
        def users(self):
            class _U:
                def getProfile(self, userId=None):
                    class _P:
                        def execute(self):
                            return {"emailAddress": who["addr"]}
                    return _P()
            return _U()

    gmail_oauth._EMAIL_CACHE.clear()
    monkeypatch.setattr(gmail_oauth, "TOKEN_PATH", token)
    monkeypatch.setattr(gmail_oauth, "_load_creds", lambda: object())
    monkeypatch.setattr(gmail_oauth, "_libs", lambda: (None, None, None, lambda *a, **k: _Svc()))

    assert gmail_oauth.connected_email() == "first@example.com"
    who["addr"] = "second@example.com"
    assert gmail_oauth.connected_email() == "first@example.com", "not actually cached"

    import os
    st = token.stat()
    os.utime(token, (st.st_atime + 10, st.st_mtime + 10))   # a new token was written
    assert gmail_oauth.connected_email() == "second@example.com", (
        "reconnecting a different Gmail account kept serving the old address")


def test_schema_setup_does_not_repeat_on_every_call(db):
    """199 of the original 313 statements were CREATE/PRAGMA re-run per request."""
    from applypilot import web_dashboard as wd
    _seed(db, jobs=3, contacts_per_job=2)
    wd._status_payload()

    schema = []

    def trace(stmt):
        if re.match(r"\s*(CREATE|PRAGMA|ALTER)", stmt or "", re.I):
            schema.append(stmt.strip()[:60])
    db.set_trace_callback(trace)
    try:
        wd._status_payload()
    finally:
        db.set_trace_callback(None)
    assert not schema, f"schema statements re-ran on a warm connection: {schema[:5]}"


def test_schema_memo_is_per_connection_not_global(tmp_path, monkeypatch):
    """A fresh connection must still build its schema — otherwise a new DB comes up empty.

    This is the failure the memo could plausibly cause, so it gets its own test.
    """
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    conn = database.init_db(path)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    database.close_connection(path)          # new connection -> memo starts empty again
    conn2 = database.get_connection(path)
    store.init_contacts(conn2)
    assert conn2.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0


def test_match_many_agrees_with_match(db):
    """Two implementations of one rule; a test has to force them to agree.

    CLAUDE.md §Lessons: every duplicated rule in this codebase has drifted eventually.
    """
    _seed(db, jobs=1, contacts_per_job=1)
    db.execute("INSERT INTO connections (name_norm, full_name, company, company_norm) "
               "VALUES ('dup name','Dup Name','Arm','arm')")
    db.execute("INSERT INTO connections (name_norm, full_name, company, company_norm) "
               "VALUES ('dup name','Dup Name','Acme','acme')")
    db.commit()
    names = ["Person 0", "Dup Name", "Nobody Here", None, ""]
    for company in ("Acme", "Arm", None):
        bulk = connections.match_many(names, company, db)
        for n in names:
            assert (connections.match(n, company, db) or {}) == (bulk.get(n or "") or {}), \
                f"match/match_many disagree for {n!r} at {company!r}"
