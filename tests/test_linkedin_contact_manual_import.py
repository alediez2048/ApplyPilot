"""NET-7 manual LinkedIn contact import: Apollo identity-enriched."""

from __future__ import annotations

import applypilot.database as database
from applypilot.networking import service, store


def _db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    return database.get_connection(path)


def test_parse_manual_linkedin_contacts_accepts_forgiving_lines():
    people = service.parse_manual_linkedin_contacts(
        "Jane Smith - Senior Technical Recruiter - https://www.linkedin.com/in/jane-smith/\n"
        "Riley Gomez | Talent Acquisition Partner | https://linkedin.com/in/riley-gomez,\n"
        "https://www.linkedin.com/in/pat-sourcer"
    )

    assert people == [
        {
            "full_name": "Jane Smith",
            "title": "Senior Technical Recruiter",
            "linkedin_url": "https://www.linkedin.com/in/jane-smith/",
        },
        {
            "full_name": "Riley Gomez",
            "title": "Talent Acquisition Partner",
            "linkedin_url": "https://linkedin.com/in/riley-gomez",
        },
        {
            "full_name": "Pat Sourcer",
            "title": "",
            "linkedin_url": "https://www.linkedin.com/in/pat-sourcer",
        },
    ]


def test_parse_manual_linkedin_contacts_accepts_linkedin_result_cards():
    people = service.parse_manual_linkedin_contacts(
        "Charles Lukasiewicz  • 2nd\n\n"
        "Talent Aquisition at Charles Schwab\n\n"
        "Denver Metropolitan Area\n\n"
        "Connect\n"
        "Current: Talent Advisor - Corporate Functions at Charles Schwab\n\n"
        "Charles Todd is open to work\n"
        "Charles Todd • 2nd\n\n"
        "Corporate Recruiter - direct corporate clients only\n\n"
        "Oak Ridge, Tennessee, United States\n\n"
        "Connect\n"
    )

    assert people[:2] == [
        {
            "full_name": "Charles Lukasiewicz",
            "title": "Talent Aquisition at Charles Schwab",
            "linkedin_url": "",
        },
        {
            "full_name": "Charles Todd",
            "title": "Corporate Recruiter - direct corporate clients only",
            "linkedin_url": "",
        },
    ]
    assert "Denver Metropolitan Area" not in {p["full_name"] for p in people}
    assert "Product Strategy | Ops Manager at Yuno for the last 4 months" not in {
        p["full_name"] for p in people
    }


def test_manual_linkedin_contacts_enrich_by_identity_and_keep_source(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    job = {"url": "http://jobs.example/spacex/1", "title": "Flight Software Engineer", "company": "SpaceX"}

    from applypilot.networking import apollo

    seen = {}

    def enrich(people):
        seen["people"] = people
        return {people[0]["key"]: {
            "email": "jane@spacex.com",
            "email_status": "verified",
            "linkedin_url": people[0]["linkedin_url"],
            "apollo_id": "apollo-1",
            "full_name": "Jane Smith",
        }}

    monkeypatch.setattr(apollo, "match_by_identity", enrich)
    res = service.import_linkedin_contacts_for_job(job, [{
        "full_name": "Jane Smith",
        "title": "Senior Technical Recruiter",
        "linkedin_url": "https://www.linkedin.com/in/jane-smith",
    }], draft=False)

    assert res["found"] == 1 and res["revealed"] == 1
    assert seen["people"][0]["linkedin_url"].endswith("jane-smith")
    rows = store.get_contacts_for_job(job["url"], conn)
    assert len(rows) == 1
    assert rows[0]["source"] == "linkedin_manual_recruiter"
    assert rows[0]["match_reason"] == "manual LinkedIn recruiter import"
    assert rows[0]["email"] == "jane@spacex.com"


def test_manual_linkedin_import_keeps_operator_selected_non_recruiters(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    job = {"url": "http://jobs.example/yuno/1", "title": "AI Engineer", "company": "Yuno"}

    from applypilot.networking import apollo
    monkeypatch.setattr(apollo, "match_by_identity", lambda people: {})

    res = service.import_linkedin_contacts_for_job(job, [{
        "full_name": "Simon Martinez",
        "title": "VP of Artificial Intelligence",
        "linkedin_url": "",
    }, {
        "full_name": "Isabela González González",
        "title": "HRBP & Global Talent Acquisition at Yuno",
        "linkedin_url": "",
    }], draft=False)

    assert res["found"] == 2
    rows = store.get_contacts_for_job(job["url"], conn)
    by_name = {r["full_name"]: r for r in rows}
    assert by_name["Simon Martinez"]["source"] == "linkedin_manual"
    assert by_name["Simon Martinez"]["match_reason"] == "manual LinkedIn import"
    assert by_name["Isabela González González"]["source"] == "linkedin_manual_recruiter"


def test_manual_linkedin_contact_apollo_miss_still_persists_linkedin_contact(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    job = {"url": "http://jobs.example/acme/1", "title": "AI Engineer", "company": "Acme"}

    from applypilot.networking import apollo
    monkeypatch.setattr(apollo, "match_by_identity", lambda people: {})

    res = service.import_linkedin_contacts_for_job(job, [{
        "full_name": "Riley Gomez",
        "title": "Technical Recruiter",
        "linkedin_url": "https://www.linkedin.com/in/riley-gomez",
    }], draft=False)

    assert res["found"] == 1 and res["revealed"] == 0
    rows = store.get_contacts_for_job(job["url"], conn)
    assert len(rows) == 1
    assert rows[0]["email"] is None
    assert rows[0]["email_status"] == "none"
    assert rows[0]["linkedin_url"].endswith("riley-gomez")


def test_manual_linkedin_contact_import_dedupes_existing_linkedin_contact(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    job = {"url": "http://jobs.example/acme/1", "title": "AI Engineer", "company": "Acme"}
    store.upsert_contact({
        "job_url": job["url"],
        "full_name": "Riley Gomez",
        "title": "Technical Recruiter",
        "company": "Acme",
        "linkedin_url": "https://www.linkedin.com/in/riley-gomez/",
        "source": "apollo",
    }, conn)

    from applypilot.networking import apollo
    monkeypatch.setattr(apollo, "match_by_identity",
                        lambda people: (_ for _ in ()).throw(AssertionError("should not enrich dupes")))

    res = service.import_linkedin_contacts_for_job(job, [{
        "full_name": "Riley Gomez",
        "title": "Technical Recruiter",
        "linkedin_url": "https://www.linkedin.com/in/riley-gomez",
    }], draft=False)

    assert "already known" in res["note"]
    assert len(store.get_contacts_for_job(job["url"], conn)) == 1
