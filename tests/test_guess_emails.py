"""Company email guessing from already-known contacts."""

from __future__ import annotations

import applypilot.database as database
from applypilot.networking import service, store


def _db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    return database.get_connection(path)


def test_guess_missing_emails_uses_company_first_last_pattern(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    job = {"url": "http://jobs.example/schwab/1", "title": "TPM", "company": "Charles Schwab"}
    conn.execute(
        "INSERT INTO jobs (url, title, company, apply_status) VALUES (?,?,?,?)",
        (job["url"], job["title"], job["company"], "applied"),
    )
    store.init_contacts(conn)
    store.upsert_contact({
        "job_url": job["url"],
        "full_name": "Charles Lukasiewicz",
        "company": "Charles Schwab",
        "email": "charles.lukasiewicz@schwab.com",
        "email_status": "verified",
    }, conn)
    missing = store.upsert_contact({
        "job_url": job["url"],
        "full_name": "Kelly Day",
        "company": "Charles Schwab",
        "email_status": "none",
    }, conn)

    monkeypatch.setattr(service, "_draft_and_store", lambda *a, **k: None)
    res = service.guess_missing_emails_for_job(job)

    assert res["guessed"] == 1
    row = store.get_contact(missing, conn)
    assert row["email"] == "kelly.day@schwab.com"
    assert row["email_status"] == "unverified"
    assert "charles.lukasiewicz@schwab.com" in row["verify_note"]


def test_guess_uses_current_card_contacts_even_with_old_space_stamp(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    job = {"url": "target:sheet-search:yuno", "title": "Yuno", "company": "Yuno",
           "space_id": "sheet-search"}
    conn.execute(
        "INSERT INTO jobs (url, title, company, apply_status, space_id) VALUES (?,?,?,?,?)",
        (job["url"], job["title"], job["company"], "imported", job["space_id"]),
    )
    store.init_contacts(conn)
    store.upsert_contact({
        "job_url": job["url"],
        "full_name": "Simon Martinez",
        "company": "Yuno",
        "email": "simon@y.uno",
        "email_status": "verified",
    }, conn)
    missing = store.upsert_contact({
        "job_url": job["url"],
        "full_name": "Andrea Bautista",
        "company": "Yuno",
        "email_status": "none",
    }, conn)

    monkeypatch.setattr(service, "_draft_and_store", lambda *a, **k: None)
    res = service.guess_missing_emails_for_job(job)

    assert res["guessed"] == 1
    row = store.get_contact(missing, conn)
    assert row["email"] == "andrea@y.uno"
    assert row["space_id"] == "job-search"


def test_guess_missing_emails_refuses_ambiguous_patterns(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    job = {"url": "http://jobs.example/acme/1", "title": "Engineer", "company": "Acme"}
    conn.execute(
        "INSERT INTO jobs (url, title, company, apply_status) VALUES (?,?,?,?)",
        (job["url"], job["title"], job["company"], "applied"),
    )
    store.init_contacts(conn)
    store.upsert_contact({
        "job_url": job["url"],
        "full_name": "Jane Smith",
        "company": "Acme",
        "email": "jane.smith@acme.com",
        "email_status": "verified",
    }, conn)
    target = store.upsert_contact({
        "job_url": job["url"],
        "full_name": "Pat Gomez",
        "company": "Acme",
        "email": "pgomez@acme.com",
        "email_status": "verified",
    }, conn)
    missing = store.upsert_contact({
        "job_url": job["url"],
        "full_name": "Riley Stone",
        "company": "Acme",
        "email_status": "none",
    }, conn)

    res = service.guess_missing_emails_for_job(job)

    assert res["guessed"] == 0
    assert "ambiguous" in res["note"].lower()
    assert store.get_contact(missing, conn)["email"] in (None, "")
    assert store.get_contact(target, conn)["email"] == "pgomez@acme.com"
