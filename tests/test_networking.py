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
    # careers-portal subdomain is stripped to the corporate domain (what Apollo needs)
    assert derive.derive_domain({"application_url": "https://careers.affirm.com/x"}) == "affirm.com"
    assert derive.derive_domain({"application_url": "https://careers.amd.com/careers-home/jobs/1"}) == "amd.com"


def test_derive_company_yc_listing_is_the_startup_not_yc():
    """A YC listing is the startup's job — searching 'Y Combinator' finds YC's own recruiters."""
    job = {
        "company": "Ycombinator", "site": "Ycombinator",
        "url": "https://www.ycombinator.com/companies/hamming-ai/jobs/XTCQPuO-product-engineer",
    }
    assert derive.derive_company(job) == "Hamming AI"
    # and the board's own domain must never be handed to Apollo
    assert derive.derive_domain(job) is None


def test_derive_company_never_returns_a_board_name():
    """With no employer signal at all, report nothing rather than the board."""
    assert derive.derive_company({"company": "Ycombinator", "site": "Ycombinator"}) is None


def test_employer_slug_from_ats_urls():
    f = derive.employer_slug_from_url
    assert f("https://job-boards.greenhouse.io/devrev/jobs/5823633004") == "devrev"
    assert f("https://jobs.ashbyhq.com/webAI/c55123d9") == "webAI"
    assert f("https://jobs.lever.co/acme/1234") == "acme"
    assert f("https://www.workatastartup.com/companies/openai") == "openai"
    # no employer in the path -> no slug
    assert f("https://www.ycombinator.com/jobs") is None
    assert f("https://careers.affirm.com/jobs/1") is None
    assert f(None) is None


def test_titleize_slug_preserves_real_casing():
    assert derive.titleize_slug("hamming-ai") == "Hamming AI"
    assert derive.titleize_slug("openai") == "OpenAI"
    assert derive.titleize_slug("devrev") == "Devrev"
    # Ashby preserves the employer's own capitalization — .title() would flatten it
    assert derive.titleize_slug("webAI") == "webAI"


def test_derive_domain_strips_www_by_prefix_not_charset():
    """lstrip('www.') is a character set — it ate the leading w of w-initial domains."""
    assert derive.derive_domain({"application_url": "https://webai.com/careers/1"}) == "webai.com"
    assert derive.derive_domain({"application_url": "https://www.walmart.com/jobs/1"}) == "walmart.com"


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


# ── outreach drafting ───────────────────────────────────────────────────────

def test_draft_email_uses_llm_and_falls_back_subject(monkeypatch):
    from applypilot.networking import outreach

    class _C:
        def chat(self, msgs, **k):
            return ('{"subject": "", "body": "Hi Jane, I applied for the AI role. Jorge",'
                    ' "linkedin_note": "Hi Jane, saw the AI role — would love to connect. Jorge"}')
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _C())

    d = outreach.draft_email(
        {"personal": {"full_name": "Jorge Diez", "preferred_name": "Jorge"},
         "experience": {"target_role": "AI Engineer"}, "skills_boundary": {"languages": ["Python"]}},
        {"title": "AI Solutions Engineer", "company": "Affirm", "full_description": "Build AI"},
        {"full_name": "Jane Smith", "title": "Staff AI Engineer", "match_reason": "same role"},
    )
    assert "Affirm" in d["subject"] or "AI Solutions Engineer" in d["subject"]  # fallback subject
    assert d["body"].startswith("Hi Jane")
    assert d["linkedin_note"] and len(d["linkedin_note"]) <= 300


def test_linkedin_note_capped_at_300(monkeypatch):
    from applypilot.networking import outreach

    long = "word " * 100  # 500 chars
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: type("C", (), {
        "chat": lambda self, m, **k: '{"subject":"s","body":"b","linkedin_note":"' + long + '"}'})())
    d = outreach.draft_email({}, {"title": "X"}, {"full_name": "Y"})
    assert len(d["linkedin_note"]) <= 300 and d["linkedin_note"].endswith("…")


def test_draft_email_empty_body_raises(monkeypatch):
    from applypilot.networking import outreach

    class _C:
        def chat(self, msgs, **k):
            return '{"subject": "hi", "body": ""}'
    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _C())
    import pytest
    with pytest.raises(ValueError):
        outreach.draft_email({}, {"title": "X"}, {"full_name": "Y"})


def test_board_host_match_is_label_wise_not_substring():
    """Regression: `any(b in host)` rejected real employers whose name CONTAINS a board name."""
    f = derive.derive_domain
    # "lever" is inside "clever", "jobs" inside "jobsight" — these are real employers
    assert f({"application_url": "https://careers.clever.com/jobs/1"}) == "clever.com"
    assert f({"application_url": "https://jobs.leverage.io/x"}) == "leverage.io"
    assert f({"application_url": "https://www.jobsight.com/apply/9"}) == "jobsight.com"
    # an employer's own careers portal is not a board, even on a jobs./careers. subdomain
    assert f({"application_url": "https://jobs.stripe.com/positions/123"}) == "stripe.com"
    # actual boards still rejected
    assert f({"application_url": "https://jobs.lever.co/acme/1"}) is None
    assert f({"application_url": "https://job-boards.greenhouse.io/affirm/jobs/1"}) is None
    assert f({"application_url": "https://jobs.ashbyhq.com/webAI/x"}) is None


def test_generic_host_labels_never_become_a_company_name():
    """'job-boards.greenhouse.io' must not yield the company 'Job Boards'."""
    got = derive.derive_company({"site": "Indeed",
                                 "url": "https://job-boards.greenhouse.io/affirm/jobs/1"})
    assert got is None or got.lower() == "affirm"


# ── org disambiguation (Apollo name search is fuzzy) ────────────────────────
# Regression: asking Apollo for "WRITER" returns Writer (writer.com), Writer Corporation,
# The Writer, Content Writer and a freelance resume writer — and ALL five org ids were
# passed to people-search, mixing five companies' staff onto one job.

_WRITER_ORGS = [
    {"id": "1", "name": "Muhammad Ejaz | ATS Resume Writer", "domain": "dailysuccess.org"},
    {"id": "2", "name": "WRITER", "domain": "writer.com"},
    {"id": "3", "name": "Content Writer", "domain": "contentwriter.co"},
    {"id": "4", "name": "The Writer", "domain": "thewriter.com"},
    {"id": "5", "name": "Writer Corporation", "domain": "writercorporation.com"},
]


def test_resolve_orgs_keeps_only_the_real_employer(monkeypatch):
    from applypilot.networking import providers
    monkeypatch.setattr(apollo, "company_lookup", lambda n, per_page=5: _WRITER_ORGS)
    orgs, domain = providers.resolve_orgs("WRITER")
    assert [o["id"] for o in orgs] == ["2"]
    assert domain == "writer.com"


def test_resolve_orgs_rejects_same_prefix_different_company(monkeypatch):
    """'Affirm Health' and 'Affirm Partners' are not Affirm."""
    from applypilot.networking import providers
    monkeypatch.setattr(apollo, "company_lookup", lambda n, per_page=5: [
        {"id": "a", "name": "Affirm", "domain": "affirm.com"},
        {"id": "b", "name": "Affirm Partners", "domain": "affirmpartners.com"},
        {"id": "c", "name": "Affirm Health", "domain": "affirmhealth.com"},
    ])
    orgs, domain = providers.resolve_orgs("Affirm")
    assert [o["id"] for o in orgs] == ["a"] and domain == "affirm.com"


def test_search_falls_back_to_keywords_when_no_org_matches(monkeypatch):
    """Apollo's name search misses some employers entirely (BetterUp) — still find people."""
    from applypilot.networking import providers
    monkeypatch.setattr(providers, "active", lambda: "apollo")
    monkeypatch.setattr(apollo, "company_lookup", lambda n, per_page=5: [
        {"id": "x", "name": "BetterUp Government", "domain": ""},
        {"id": "y", "name": "Better Up Now", "domain": "betterupnow.com"},
    ])
    seen = {}

    def fake_people(**kw):
        seen.update(kw)
        return [{"apollo_id": "p1", "full_name": "Someone"}]
    monkeypatch.setattr(apollo, "search_people", fake_people)
    out = providers.search("BetterUp", None, "PM", ["Recruiter"])
    assert out and seen["organization_ids"] is None
    assert seen["keywords"] == "BetterUp"       # fallback, not an empty result


def test_search_passes_only_matched_org_ids(monkeypatch):
    from applypilot.networking import providers
    monkeypatch.setattr(providers, "active", lambda: "apollo")
    monkeypatch.setattr(apollo, "company_lookup", lambda n, per_page=5: _WRITER_ORGS)
    seen = {}
    monkeypatch.setattr(apollo, "search_people",
                        lambda **kw: (seen.update(kw), [{"apollo_id": "p"}])[1])
    out = providers.search("WRITER", None, "Eng", ["Recruiter"])
    assert seen["organization_ids"] == ["2"]        # not all five
    assert out[0]["employer_domain"] == "writer.com"


def test_email_domain_cross_check():
    """Moved into networking/verify.py; None now means "no evidence", not "fine"."""
    from applypilot.networking.verify import email_domain_agrees
    assert email_domain_agrees("miles.parroco@writer.com", "writer.com") is True
    assert email_domain_agrees("payal@writercorporation.com", "writer.com") is False
    assert email_domain_agrees("x@eu.writer.com", "writer.com") is True    # subdomain
    # no address / no known employer is not evidence either way
    assert email_domain_agrees("", "writer.com") is None
    assert email_domain_agrees(None, "writer.com") is None
    assert email_domain_agrees("a@b.com", "") is None
