"""An ATS-hosted job has no employer domain, so Apollo must be given one.

2026-07-30, a live Wander application (ats.rippling.com/wander/...) found NOBODY. The dashboard
note read:

    15 found, 11 with email (ok; dropped 15 who work elsewhere)

Every layer behaved correctly and the result was still useless:

  * derive_domain returned None — ats.rippling.com is the VENDOR's domain, not Wander's;
  * with no domain Apollo fell back to a fuzzy NAME search, and "Wander" is a common word:
    Apollo lists wearewander.co.uk, welovewander.com, wander.ch (Wander AG) and wandermaps.com,
    and the real employer — wander.com — is not among them;
  * so every candidate worked at Wander AG, and verification correctly dropped all 15.

Meanwhile Apollo held the CEO, President, CMO and several engineers at wander.com the whole
time. Two fixes, both needed: recover the domain, and don't let a title filter empty a company
we have already positively identified.
"""

from __future__ import annotations

import pytest

from applypilot.networking import apollo, providers


@pytest.fixture(autouse=True)
def apollo_active(monkeypatch):
    monkeypatch.setattr(providers, "active", lambda: "apollo")


# ── recovering the domain ────────────────────────────────────────────────────────────────

def test_a_guess_is_accepted_only_when_apollo_corroborates_it(monkeypatch):
    """The Wander case. wander.com is not in Apollo's name search, but asking "who works at
    wander.com" returns people whose employer IS Wander."""
    def fake(domains=None, **kw):
        if domains == ["wander.com"]:
            return [{"full_name": "John", "company": "Wander"}]
        return []

    monkeypatch.setattr(apollo, "search_people", fake)
    assert providers.confirm_employer_domain("Wander", "wander") == "wander.com"


def test_a_domain_hosting_a_DIFFERENT_company_is_refused(monkeypatch):
    """The whole reason this is a corroborated guess and not a plain one. A blind
    "<slug>.com" would hand Apollo a domain belonging to somebody else and return real humans
    at the wrong company — the failure mode this codebase has shipped four times."""
    monkeypatch.setattr(apollo, "search_people",
                        lambda domains=None, **kw: [{"full_name": "X", "company": "Arm Holdings"}])
    assert providers.confirm_employer_domain("Armanino", "armanino") == ""


def test_a_domain_with_nobody_on_it_is_refused(monkeypatch):
    monkeypatch.setattr(apollo, "search_people", lambda **kw: [])
    assert providers.confirm_employer_domain("Wander", "wander") == ""


def test_the_probe_asks_who_works_here_at_all_not_who_has_the_title(monkeypatch):
    """A narrow title list returns nobody at a small company, which would make a CORRECT guess
    look wrong. Confirming the domain is a different question from finding the right person."""
    seen: list = []

    def fake(domains=None, titles=None, **kw):
        seen.append(titles)
        return [{"full_name": "A", "company": "Wander"}]

    monkeypatch.setattr(apollo, "search_people", fake)
    providers.confirm_employer_domain("Wander", "wander")
    assert seen and all(t is None for t in seen), f"probe filtered by title: {seen}"


def test_it_stops_at_the_first_confirmed_domain(monkeypatch):
    """Each probe is a paid Apollo call; walking every TLD after a hit is waste."""
    calls: list = []

    def fake(domains=None, **kw):
        calls.append(domains[0])
        return [{"full_name": "A", "company": "Wander"}]

    monkeypatch.setattr(apollo, "search_people", fake)
    assert providers.confirm_employer_domain("Wander", "wander") == "wander.com"
    assert calls == ["wander.com"], f"kept probing after a hit: {calls}"


def test_the_url_slug_is_preferred_over_the_display_name(monkeypatch):
    """The slug is the employer's own identifier. "Hamming AI" normalises to hammingai, but the
    slug says hamming-ai -> hammingai too; where they differ the slug is the better bet."""
    tried: list = []

    def fake(domains=None, **kw):
        tried.append(domains[0])
        return []

    monkeypatch.setattr(apollo, "search_people", fake)
    providers.confirm_employer_domain("Wander Inc", "wander")
    assert tried[0] == "wander.com", f"did not try the slug first: {tried[:3]}"


def test_no_company_means_no_probing(monkeypatch):
    monkeypatch.setattr(apollo, "search_people",
                        lambda **kw: pytest.fail("probed Apollo with no company name"))
    assert providers.confirm_employer_domain(None, None) == ""
    assert providers.confirm_employer_domain("", "") == ""


def test_a_probe_failure_is_not_fatal(monkeypatch):
    """One bad domain must not abort the whole search."""
    def fake(domains=None, **kw):
        if domains == ["wander.com"]:
            raise RuntimeError("apollo 500")
        return [{"full_name": "A", "company": "Wander"}]

    monkeypatch.setattr(apollo, "search_people", fake)
    assert providers.confirm_employer_domain("Wander", "wander") == "wander.io"


# ── not letting a title filter empty a known company ─────────────────────────────────────

def test_a_title_filter_that_matches_nobody_widens_to_the_whole_company(monkeypatch):
    """At wander.com the synonyms for "Forward Deployed Engineer" matched 0 of 10 listed
    people. On a 50-person startup the CEO and VP Product are exactly who you want."""
    def fake(domains=None, organization_ids=None, titles=None, keywords=None, **kw):
        if titles:
            return []
        return [{"apollo_id": "1", "full_name": "John", "title": "Founder, CEO",
                 "company": "Wander"}]

    monkeypatch.setattr(apollo, "search_people", fake)
    got = providers.search("Wander", "wander.com", "Forward Deployed Engineer",
                           ["forward deployed engineer"])
    assert [c["full_name"] for c in got] == ["John"]


def test_widening_only_happens_when_the_company_is_already_known(monkeypatch):
    """With no domain and no matched org, the query is a KEYWORD guess. Dropping the title
    filter there returns arbitrary people from anywhere — far worse than returning nothing."""
    calls: list = []

    def fake(domains=None, organization_ids=None, titles=None, keywords=None, **kw):
        calls.append({"titles": titles, "keywords": keywords})
        return []

    monkeypatch.setattr(apollo, "search_people", fake)
    monkeypatch.setattr(providers, "resolve_orgs", lambda *a, **k: ([], ""))
    providers.search("Wander", None, "Engineer", ["engineer"])
    assert len(calls) == 1, f"widened an unanchored keyword search: {calls}"


def test_a_successful_titled_search_is_not_repeated(monkeypatch):
    """The widening is a fallback, not a second query on every search."""
    calls: list = []

    def fake(domains=None, organization_ids=None, titles=None, **kw):
        calls.append(titles)
        return [{"apollo_id": "1", "full_name": "A", "company": "Wander"}]

    monkeypatch.setattr(apollo, "search_people", fake)
    providers.search("Wander", "wander.com", "Engineer", ["engineer"])
    assert len(calls) == 1, f"searched twice despite a hit: {calls}"
