"""Import and contact discovery must name the SAME employer.

`_infer_company` decides the job's title and the company that tailoring and the cover letter
are written against. It re-implemented host handling as `domain[-2]` — the REGISTRABLE label —
so an ATS tenant URL imported as the ATS:

    salesforce.wd12.myworkdayjobs.com  ->  "Myworkdayjobs"

A live Salesforce application was titled "Myworkdayjobs uploaded job", tailored against
"Myworkdayjobs", and the validator warned the cover letter never named the company. Its own
docstring promised the rules live in networking.derive so import and discovery agree; two
implementations of "who is the employer" is how they stopped agreeing.
"""

from __future__ import annotations

import pytest

from applypilot.networking import derive
from applypilot.web_dashboard import _infer_company

CASES = [
    ("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/"
     "California---San-Francisco/Forward-Deployed-Engineer--All-Levels-_JR349466", "Salesforce"),
    ("https://job-boards.greenhouse.io/affirm/jobs/7778204003", "Affirm"),
    ("https://jobs.ashbyhq.com/Zello/2fa8cd4a/application", "Zello"),
    ("https://www.ycombinator.com/companies/hamming-ai/jobs/XTCQPuO-product-engineer",
     "Hamming AI"),
    ("https://careers.arm.com/job/austin/project-manager/33099/98187464416", "Arm"),
]


@pytest.mark.parametrize("url,expected", CASES)
def test_import_names_the_employer_not_the_ats(url, expected):
    assert _infer_company(url) == expected


@pytest.mark.parametrize("url,_e", CASES)
def test_import_and_contact_discovery_never_disagree(url, _e):
    """They drive different things — the job title/tailoring vs the Apollo search — so a
    disagreement means the résumé is written for one company and contacts found at another."""
    assert _infer_company(url) == derive.derive_company({"url": url, "application_url": url})


def test_an_unresolvable_url_yields_NO_company_rather_than_a_placeholder():
    """This test used to assert `== "Uploaded"`, and that is the sharpest thing in this file.

    The placeholder was not an oversight sitting in the code — it was PINNED HERE as correct
    behaviour, so anyone who removed it broke a green test and put it back. Meanwhile
    `_infer_company` writes both `company` AND `site`, and the cover letter addressed itself to
    `site`, so six real applications went out saying "Dear Uploaded Hiring Team" — one of them to
    Google, one to LegalZoom saying "Dear Jobvite", one to Texas Children's Hospital saying "Dear
    Oraclecloud".

    "" is a real answer and every consumer must handle it. A placeholder that looks like a name
    is worse than no name, because everything downstream treats it as a fact and only a human
    reading the final PDF can tell.
    """
    assert _infer_company("not-a-url") == ""
    assert _infer_company("https://www.linkedin.com/jobs/view/4449383611/") == ""
