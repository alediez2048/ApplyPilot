"""NET-1 tests: company/domain derivation, ranking, store, Apollo client (mocked)."""

from __future__ import annotations

import httpx

from applypilot.networking import apollo, derive, rank, store


# ── derive ──────────────────────────────────────────────────────────────────

def test_derive_company_prefers_stored_over_board():
    assert derive.derive_company({"company": "Affirm", "site": "Indeed"}) == "Affirm"


def test_derive_company_ignores_board_name_in_company_field():
    # if company somehow holds a board name, fall through to other signals
    job = {"company": "Indeed", "application_url": "https://careers.affirm.com/jobs/1"}
    assert derive.derive_company(job).lower() == "affirm"


def test_derive_company_from_json_ld():
    desc = 'blah {"hiringOrganization": {"@type":"Organization","name":"Acme Corp"}} blah'
    assert derive.derive_company({"full_description": desc, "site": "LinkedIn"}) == "Acme Corp"


def test_derive_company_from_careers_hostname():
    job = {"site": "Indeed", "application_url": "https://jobs.stripe.com/positions/123"}
    assert derive.derive_company(job).lower() == "stripe"


def test_derive_domain_skips_board_hosts():
    # greenhouse host is an ATS, not the employer domain
    assert derive.derive_domain({"url": "https://job-boards.greenhouse.io/affirm/jobs/1"}) is None
    assert derive.derive_domain({"application_url": "https://careers.affirm.com/x"}) == "careers.affirm.com"


# ── rank ────────────────────────────────────────────────────────────────────

def test_role_to_person_titles_includes_synonyms_and_recruiters():
    titles = rank.role_to_person_titles("Senior Technical Product Manager")
    assert "Senior Technical Product Manager" in titles
    assert "Technical Product Manager" in titles          # de-seniored
    assert any("Recruiter" in t for t in titles)          # recruiter always added


def test_select_guarantees_a_hiring_contact_and_ranks_peers():
    cands = [
        {"apollo_id": "1", "full_name": "A", "title": "Staff Software Engineer"},
        {"apollo_id": "2", "full_name": "B", "title": "Software Engineer"},
        {"apollo_id": "3", "full_name": "C", "title": "Technical Recruiter"},
        {"apollo_id": "4", "full_name": "D", "title": "Marketing Lead"},
    ]
    picked = rank.select(cands, "Senior Software Engineer", n=3)
    reasons = {c["full_name"]: c["match_reason"] for c in picked}
    assert "C" in reasons and reasons["C"] == "recruiter"   # hiring contact guaranteed
    assert any(reasons.get(n) == "same role" for n in ("A", "B"))
    assert len(picked) == 3


def test_select_empty():
    assert rank.select([], "Engineer") == []


# ── store ───────────────────────────────────────────────────────────────────

def test_contact_id_is_delimited_and_stable():
    a = store.contact_id("http://j/1", "linkedin.com/in/x", "Jane")
    b = store.contact_id("http://j/1", "linkedin.com/in/x", "Jane")
    assert a == b
    # delimiter avoids the classic ab|c vs a|bc collision
    assert store.contact_id("http://j/1a", "b", "c") != store.contact_id("http://j/1", "ab", "c")


def test_store_upsert_and_fetch(tmp_path, monkeypatch):
    import applypilot.database as database
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)   # get_connection() reads this module global
    database.close_connection(db)
    database.init_db(db)
    store.init_contacts()

    cid = store.upsert_contact({
        "job_url": "http://j/1", "full_name": "Jane Smith", "title": "Staff AI Engineer",
        "email": "jane@x.com", "email_status": "verified", "source": "apollo",
    })
    rows = store.get_contacts_for_job("http://j/1")
    assert len(rows) == 1 and rows[0]["email"] == "jane@x.com"

    # idempotent update: same id, no duplicate row
    store.upsert_contact({"id": cid, "job_url": "http://j/1", "full_name": "Jane Smith",
                          "title": "Staff AI Engineer, Platform"})
    rows = store.get_contacts_for_job("http://j/1")
    assert len(rows) == 1 and rows[0]["title"] == "Staff AI Engineer, Platform"
    assert rows[0]["email"] == "jane@x.com"  # preserved (not overwritten with None)


# ── apollo (mocked) ─────────────────────────────────────────────────────────

def test_email_status_mapping():
    assert apollo._map_email_status("verified", "a@b.com") == "verified"
    assert apollo._map_email_status("extrapolated", "a@b.com") == "unverified"
    assert apollo._map_email_status("verified", None) == "none"


def test_search_people_parses_and_masks(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "k")

    def fake_post(url, headers=None, json=None, timeout=None):
        return httpx.Response(200, json={"people": [
            {"id": "1", "name": "Jane Smith", "title": "Staff AI Engineer",
             "seniority": "senior", "organization": {"name": "Affirm"}},
        ]})
    monkeypatch.setattr(httpx, "post", fake_post)

    out = apollo.search_people(domains=["affirm.com"], titles=["AI Engineer"])
    assert out[0]["apollo_id"] == "1" and out[0]["full_name"] == "Jane Smith"
    assert "email" not in out[0]  # masked in search


def test_bulk_enrich_reveals(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "k")

    def fake_post(url, headers=None, json=None, timeout=None):
        return httpx.Response(200, json={"matches": [
            {"id": "1", "email": "jane@affirm.com", "email_status": "verified",
             "linkedin_url": "https://linkedin.com/in/jane"},
        ]})
    monkeypatch.setattr(httpx, "post", fake_post)

    rev = apollo.bulk_enrich(["1"])
    assert rev["1"]["email"] == "jane@affirm.com"
    assert rev["1"]["email_status"] == "verified"
    assert rev["1"]["linkedin_url"].endswith("/jane")


def test_probe_no_key(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    ok, msg = apollo.probe()
    assert ok is False and "not set" in msg


def test_search_people_no_key_returns_empty(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    assert apollo.search_people(domains=["x.com"], titles=["Eng"]) == []
