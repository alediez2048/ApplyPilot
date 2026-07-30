"""An ATS host must never become the employer name.

2026-07-30, on a live Wander application:

    https://ats.rippling.com/wander/jobs/fa15dee0-...

imported as company "Ats". The job was titled "Ats uploaded job", the résumé and cover letter
were written and filed as `Ats_Ats_uploaded_job_*`, and the validator warned "Cover letter never
names the company (Ats)". The materials were generated — they were just addressed to nobody.

Why: `_ATS_PATH_SLUG` had no rippling.com rule, so the employer slug in the path was never read.
`_host_label` then saw ats.rippling.com, rejected 'rippling' as a board, and fell back to the
FIRST label — 'ats'.

Two independent holes, so two fixes: the missing path rules, and 'ats'/'apply' as generic host
labels that can never be a company on their own. The second is the general guard — it holds for
any future ats.<board>.com without needing a rule per vendor.
"""

from __future__ import annotations

import pytest

from applypilot.networking import derive
from applypilot.web_dashboard import _infer_company

WANDER = "https://ats.rippling.com/wander/jobs/fa15dee0-a8f6-4923-9f4d-3301df0ef387?jobSite=LinkedIn"


def test_the_rippling_job_resolves_to_wander():
    """The exact bug."""
    assert derive.employer_slug_from_url(WANDER) == "wander"
    assert derive.derive_company({"url": WANDER, "application_url": WANDER}) == "Wander"


def test_import_agrees_so_the_title_and_cover_letter_are_right():
    """_infer_company drives the job title and the company the cover letter must name."""
    assert _infer_company(WANDER) == "Wander"


@pytest.mark.parametrize("label", ["ats", "apply"])
def test_a_bare_infra_subdomain_is_never_a_company(label):
    """The general guard. Without it, every unknown ats.<vendor>.com repeats this bug — the
    fallback happily returns whatever the first host label happens to be."""
    assert label in derive._GENERIC_HOST_LABELS


def test_an_unknown_ats_host_gives_up_rather_than_inventing_a_name():
    """"Could not determine employer" is recoverable. "Ats" silently produces a cover letter
    addressed to nothing, which is worse because it looks like it worked."""
    url = "https://ats.somenewvendor.com/jobs/12345"
    got = derive.derive_company({"url": url, "application_url": url})
    assert got != "Ats", got


def test_workable_takes_the_employer_from_the_path_too():
    """apply.workable.com/<employer>/j/<id> — same shape, same missing rule."""
    url = "https://apply.workable.com/acme-robotics/j/ABC123/"
    assert derive.employer_slug_from_url(url) == "acme-robotics"


@pytest.mark.parametrize("url,expected", [
    # Host-label ATSs: the employer is the subdomain, and these must keep working.
    ("https://acme.bamboohr.com/careers/42", "Acme"),
    ("https://acme.breezy.hr/p/12345-engineer", "Acme"),
    ("https://apply.deloitte.com/en_US/careers/InviteToApply?jobId=350624", "Deloitte"),
])
def test_subdomain_employers_are_unaffected(url, expected):
    """'apply' becoming a generic label must not break apply.deloitte.com, where the employer
    is the label AFTER it."""
    assert derive.derive_company({"url": url, "application_url": url}) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/affirm/jobs/7778204003", "Affirm"),
    ("https://jobs.ashbyhq.com/Zello/2fa8cd4a/application", "Zello"),
    ("https://www.ycombinator.com/companies/hamming-ai/jobs/XTCQPuO", "Hamming AI"),
    ("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/CA/Eng_JR349466",
     "Salesforce"),
])
def test_the_existing_ats_rules_still_hold(url, expected):
    assert derive.derive_company({"url": url, "application_url": url}) == expected
