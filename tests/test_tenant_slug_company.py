"""An ATS tenant slug is not the employer's name.

Reported as "there's no way Apollo has no contacts for Yahoo". Apollo was telling the truth: the
employer had been read off the Workday tenant `ouryahoo.wd5.myworkdayjobs.com` and stored as
**Ouryahoo**, and there is no company by that name. Measured against the live API:

    company_lookup("Ouryahoo")  -> nothing
    company_lookup("Yahoo")     -> Yahoo, Yahoo Finance, Yahoo DSP, Yahoo! JAPAN, ...

After the fix the same job returns 5 contacts, all Talent Acquisition, all `high` confidence.

The tenant slug is chosen by the employer's HR team and routinely wraps the real name:
`ouryahoo`, `WellsFargoJobs`, `acme-external`. Trimming those affixes blind is NOT acceptable —
"OurCrowd" is a real company — so a trimmed variant is only accepted when the posting's own text
contains it as a whole word. That makes this corroboration, in the same shape as
`confirm_employer_domain`, rather than a guess (§Lessons 34).
"""

from __future__ import annotations

import pytest

from applypilot.networking.derive import refine_company_from_posting as refine


# ── the case that prompted it ───────────────────────────────────────────────

def test_the_workday_tenant_prefix_is_dropped():
    assert refine("Ouryahoo", "Yahoo is a global media company. Yahoo Mail serves millions.") == "Yahoo"


def test_a_tenant_suffix_is_dropped_and_the_spacing_comes_from_the_posting():
    """"wellsfargo" is written "Wells Fargo" on the page, and that is what belongs on a cover
    letter and in an Apollo query."""
    assert refine("WellsFargoJobs", "Wells Fargo is a financial services company.") == "Wells Fargo"


def test_a_real_name_is_left_alone():
    for company, text in [("Salesforce", "Salesforce is the CRM leader."),
                          ("Acme", "Acme makes anvils."),
                          ("Stanford", "Stanford is a university.")]:
        assert refine(company, text) is None, company


# ── the trap, which this shipped with on the first pass ─────────────────────

def test_a_company_that_really_starts_with_an_affix_survives():
    """§Lessons 1 for the fifth time, inside the function written to be careful about it.

    The first version tested `variant in normalised_text`. "OurCrowd" is a real company, and the
    letters of the trimmed variant are of course still inside the untrimmed name wherever the
    posting mentions it — so "crowd" matched, and outreach would have gone to strangers at a
    company called Crowd with MORE confidence than the original name had.

    Whole-word boundaries are the entire safety mechanism: in "OurCrowd" the "C" is preceded by
    a letter, so the variant is refused and the real name survives.
    """
    assert refine("OurCrowd", "OurCrowd is an investment platform. OurCrowd invests globally.") is None
    assert refine("Jobstreet", "Jobstreet is a job marketplace across Jobstreet markets.") is None


def test_a_longer_real_name_containing_the_variant_is_not_matched():
    """"Crowd" must not match inside "CrowdStrike" either — the boundary is needed on both ends."""
    assert refine("OurCrowd", "CrowdStrikes competitor. Crowdsourcing is common.") is None


def test_it_does_match_when_the_posting_genuinely_names_the_company():
    """The other half. Without this, refusing everything would also pass the tests above."""
    assert refine("Ourcrowdstrike", "CrowdStrike protects endpoints.") == "CrowdStrike"


# ── it never invents ────────────────────────────────────────────────────────

def test_a_posting_that_never_names_the_employer_changes_nothing():
    """No corroboration, no change. A wrong employer name is worse than an awkward one: it
    sends real emails to real people at a company that is not hiring."""
    assert refine("Ouryahoo", "This posting never names the employer at all.") is None
    assert refine("Ouryahoo", None) is None
    assert refine("Ouryahoo", "") is None
    assert refine(None, "Yahoo is great") is None


def test_scattered_letters_are_not_a_match():
    """Separators between characters are limited to one, so a name cannot be assembled out of
    unrelated words spread across a sentence."""
    assert refine("WellsFargoJobs", "w e l l s f a r g o") is None


@pytest.mark.parametrize("slug", ["ourx", "thea", "jobsy"])
def test_a_slug_that_is_barely_longer_than_its_affix_is_left_alone(slug):
    """Trimming "ourx" to "x" produces a one-letter company that would match almost any text."""
    assert refine(slug, "x y z the a jobs y") is None


# ── and it reaches the code that queries Apollo ─────────────────────────────

def test_contact_discovery_uses_the_refined_name(monkeypatch):
    """A refiner nobody calls is the same as no refiner — the mutation that survived when
    `role_essentials` shipped unwired."""
    import pathlib

    from applypilot.networking import service
    src = pathlib.Path(service.__file__).read_text(encoding="utf-8")
    block = src[src.index("company = derive.derive_company(job)"):]
    block = block[:block.index("domain = derive.derive_domain(")]
    assert "refine_company_from_posting" in block, (
        "the refined employer name never reaches the Apollo query")


# ── and a zero result has to say WHICH zero it is ───────────────────────────

@pytest.mark.parametrize("label,reachable,why,known,expect", [
    ("no key",         False, "APOLLO_API_KEY not set", False, "not usable"),
    ("unknown name",   True,  "ok",                     False, "no company called"),
    ("nobody matched", True,  "ok",                     True,  "returned nobody"),
])
def test_each_kind_of_zero_gets_its_own_message(monkeypatch, label, reachable, why, known, expect):
    """"no candidates from apollo (coverage or plan/key)" named three unrelated problems at once
    and pointed at none of them.

    It cost a wrong diagnosis on the Yahoo job: the real cause was an employer name no provider
    has heard of, and a missing API key would have printed the identical sentence. §Lessons 15 —
    a zero result must be as loud, and as specific, as an error.
    """
    from applypilot.networking import providers, service
    monkeypatch.setattr(providers, "active", lambda: "apollo")
    monkeypatch.setattr(providers, "probe", lambda: (reachable, why))
    monkeypatch.setattr(providers, "company_known", lambda *a, **k: known)
    monkeypatch.setattr(providers, "search", lambda *a, **k: [])

    result = service.find_contacts_for_job(
        {"url": "http://j/1", "title": "AI Ops", "company": "Ouryahoo",
         "application_url": "http://j/1"}, dry_run=True)
    assert expect in result["note"], f"{label}: {result['note']!r}"


def test_the_three_messages_are_actually_distinct(monkeypatch):
    """Three branches that all say the same thing is the bug this replaced, restated."""
    from applypilot.networking import providers, service
    monkeypatch.setattr(providers, "active", lambda: "apollo")
    monkeypatch.setattr(providers, "search", lambda *a, **k: [])
    notes = set()
    for reachable, known in ((False, False), (True, False), (True, True)):
        monkeypatch.setattr(providers, "probe", lambda r=reachable: (r, "why"))
        monkeypatch.setattr(providers, "company_known", lambda *a, k=known, **kw: k)
        notes.add(service.find_contacts_for_job(
            {"url": "http://j/1", "title": "AI Ops", "company": "Ouryahoo",
             "application_url": "http://j/1"}, dry_run=True)["note"])
    assert len(notes) == 3, f"the branches collapse to {len(notes)} message(s): {notes}"
