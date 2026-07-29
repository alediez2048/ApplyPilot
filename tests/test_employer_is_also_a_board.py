"""Some of the biggest employers are also job boards. Applying to them must still work.

2026-07-29. A Google application found no contacts at all — not even the SEVENTEEN
1st-degree LinkedIn connections in the imported CSV who work there. The search never ran:

    derive_company(job) -> None      # stored company is literally "Google"
    derive_domain(job)  -> None      # google.com is a board host
    => "could not determine employer/domain"

`_BOARD_SITES` contains 'google' because Google Jobs is a DISCOVERY SOURCE, and
`_BOARD_HOSTS` contains google.com for the same reason. That protection is right — a company
field that merely echoes the board it was scraped from must never be searched for "people who
work there". But it fires on the employer too, and Google, LinkedIn, Indeed and Glassdoor are
all companies people genuinely apply to.

The discriminator is whether the posting lives on the company's OWN domain with no other
employer named in the path:

    www.google.com/about/careers/applications/jobs/results/...  -> Google IS the employer
    www.ycombinator.com/companies/hamming-ai/jobs/...           -> YC is the BOARD, employer
                                                                   is in the path
"""

from __future__ import annotations

import pytest

from applypilot.networking import derive

GOOGLE_CAREERS = ("https://www.google.com/about/careers/applications/jobs/results/"
                  "76140305178337990-ai-outcome-customer-engineer/?src=Online/LinkedIn")


def test_google_careers_resolves_to_google_the_employer():
    """The exact failure: this returned None and killed contact discovery outright."""
    job = {"url": GOOGLE_CAREERS, "company": "Google", "site": "Google",
           "application_url": GOOGLE_CAREERS}
    assert derive.derive_company(job) == "Google"


def test_google_careers_yields_a_searchable_domain():
    """Apollo needs the domain too; without it the org lookup falls back to a fuzzy name
    search, which is what put five wrong-company people on the Zello job."""
    job = {"url": GOOGLE_CAREERS, "company": "Google", "site": "Google",
           "application_url": GOOGLE_CAREERS}
    assert derive.derive_domain(job) == "google.com"


@pytest.mark.parametrize("name,host", [
    ("LinkedIn", "https://www.linkedin.com/careers/jobs/12345"),
    ("Indeed", "https://www.indeed.com/careers/job/98765"),
    ("Glassdoor", "https://www.glassdoor.com/careers/openings/4242"),
])
def test_other_board_companies_are_employers_on_their_own_careers_site(name, host):
    """Same class of bug, not just Google."""
    job = {"url": host, "company": name, "site": name, "application_url": host}
    assert derive.derive_company(job) == name


# ── the protection this must NOT weaken ──────────────────────────────────────────────────

def test_a_yc_listing_is_still_the_startup_not_ycombinator():
    """YC hosts postings FOR other companies, and the employer is named in the path. If the
    fix keyed only on "host label == company name" this would start returning "Ycombinator"
    and search YC's own staff — the bug the board list was written to prevent."""
    url = "https://www.ycombinator.com/companies/hamming-ai/jobs/XTCQPuO-product-engineer"
    job = {"url": url, "company": "Ycombinator", "site": "ycombinator", "application_url": url}
    assert derive.derive_company(job) == "Hamming AI"


def test_a_company_field_echoing_an_unrelated_board_is_still_rejected():
    """The original protection: the company field says "Indeed" but the posting is on
    Greenhouse for someone else. Nothing corroborates Indeed, so it must not be returned."""
    url = "https://job-boards.greenhouse.io/affirm/jobs/7778204003"
    job = {"url": url, "company": "Indeed", "site": "indeed", "application_url": url}
    assert derive.derive_company(job) == "Affirm"


def test_a_board_host_with_no_employer_anywhere_still_gives_up():
    """"Could not determine employer" is the correct answer sometimes, and is far better than
    searching a board's own recruiters."""
    job = {"url": "https://www.indeed.com/viewjob?jk=abc123", "company": "Indeed",
           "site": "indeed"}
    # Nothing in the path names an employer and the path is not a careers section.
    assert derive.derive_company(job) in (None, "Indeed")


def test_a_greenhouse_url_never_yields_greenhouse_as_the_employer():
    url = "https://job-boards.greenhouse.io/devrev/jobs/5823633004"
    job = {"url": url, "company": "Greenhouse", "site": "greenhouse", "application_url": url}
    assert derive.derive_company(job) == "Devrev"


def test_the_domain_of_a_board_hosted_posting_is_still_not_the_board():
    """derive_domain must keep refusing greenhouse.io — handing that to Apollo would return
    Greenhouse employees for an Affirm job."""
    url = "https://job-boards.greenhouse.io/affirm/jobs/7778204003"
    assert derive.derive_domain({"url": url, "application_url": url}) is None


# ── the tenant-subdomain trap this fix created and then closed ───────────────────────────

SF_WORKDAY = ("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/"
              "California---San-Francisco/Forward-Deployed-Engineer--All-Levels-_JR349466")


def test_a_workday_tenant_is_the_employer_not_the_ats():
    """Caught on a live Salesforce application, and caused by the fix above.

    Enrichment discovered an application_url ending in `/apply`, and 'apply' is a careers-path
    marker, so `_company_owns_the_posting` matched the 'myworkdayjobs' label in
    salesforce.wd12.myworkdayjobs.com and declared the ATS the owner. The job went through
    tailoring as "Myworkdayjobs" and the cover letter never named Salesforce.

    The company must be the ONLY meaningful host label; a tenant prefix in front of the board
    means the board is hosting for that tenant.
    """
    job = {"url": SF_WORKDAY, "company": "Myworkdayjobs", "site": "Myworkdayjobs",
           "application_url": SF_WORKDAY + "/apply?source=LinkedIn_Jobs"}
    assert derive.derive_company(job) == "Salesforce"


def test_the_ats_name_is_rejected_even_with_an_apply_path():
    """`/apply` is a legitimate careers-path marker (employers use it), so it must not by
    itself be enough to hand ownership to whichever board label appears in the host."""
    job = {"url": SF_WORKDAY + "/apply", "company": "Workday", "site": "workday",
           "application_url": SF_WORKDAY + "/apply"}
    assert derive.derive_company(job) != "Workday"


def test_a_generic_subdomain_does_not_break_the_own_site_case():
    """careers.google.com is still Google's own — 'careers' is a generic label, not a tenant."""
    url = "https://careers.google.com/jobs/results/123-engineer/apply"
    job = {"url": url, "company": "Google", "site": "Google", "application_url": url}
    assert derive.derive_company(job) == "Google"
