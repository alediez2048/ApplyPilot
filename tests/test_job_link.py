"""URL normalisation — now used to SEE THROUGH a redirect, not to send a link.

This was written for "put the posting link in every email", which shipped and then came back out:
the drafts read badly, and a Workday URL inline in an opening sentence is the tell. Outreach
names the role instead (`tests/test_job_reference.py`).

The normalisation survived because it turned out to matter somewhere else entirely.
`derive.unwrap_job_urls` calls `clean_link` before ANY employer rule reads a hostname — without
it `jsv3.recruitics.com/redirect?rx_url=…` makes every rule describe the ad distributor, which
is how a Meta job resolved to "Recruitics" and stored three of that vendor's own recruiters.

So these tests now guard the resolver's front door, and the rule they hold is unchanged:
**strip parameters and unwrap redirects, never rewrite paths.** A dropped query param cannot
change which page loads and an unwrapped redirect resolves to its own destination — both
provable. Trimming a trailing `/application` to "recover the posting" is a guess about a URL
space we do not own, and §Lessons 32 is what that costs.
"""

from __future__ import annotations

import pytest

from applypilot.domain.joblink import KEPT_ON_PURPOSE, clean_link


# ── stripping attribution ───────────────────────────────────────────────────

@pytest.mark.parametrize("dirty,clean", [
    # Every one of these is a real stored value, trimmed.
    ("https://job-boards.greenhouse.io/affirm/jobs/7778204003?gh_src=689c81d53us",
     "https://job-boards.greenhouse.io/affirm/jobs/7778204003"),
    ("https://jobs.ashbyhq.com/betterup/544ff316?utm_source=linkedin_promoted",
     "https://jobs.ashbyhq.com/betterup/544ff316"),
    ("https://billd.bamboohr.com/careers/268?source=LinkedIn",
     "https://billd.bamboohr.com/careers/268"),
    ("https://jobs.jobvite.com/legalzoom/job/oWLtAfwu?__jvst=Job%20Board&__jvsd=LinkedIn",
     "https://jobs.jobvite.com/legalzoom/job/oWLtAfwu"),
    ("https://ats.rippling.com/wander/jobs/fa15dee0?jobSite=LinkedIn",
     "https://ats.rippling.com/wander/jobs/fa15dee0"),
    ("https://visa.wd5.myworkdayjobs.com/job/x?share_id=LinkedIn_corporate_page",
     "https://visa.wd5.myworkdayjobs.com/job/x"),
])
def test_real_tracking_params_are_removed(dirty, clean):
    assert clean_link(dirty) == clean


def test_the_malformed_stanford_url_is_repaired():
    """Two `?` in one URL. Rebuilding through urlsplit/urlencode fixes it as a side effect —
    which is worth an assertion, because a mail client may or may not linkify it and "sometimes"
    is the worst failure mode to ship."""
    dirty = ("https://careersearch.stanford.edu/hcmUI/CandidateExperience/en/sites/SLAC/job/200382"
             "?utm_medium=jobboard&utm_source=linkedin?utm_medium=search+engine&")
    assert clean_link(dirty) == (
        "https://careersearch.stanford.edu/hcmUI/CandidateExperience/en/sites/SLAC/job/200382")


def test_a_fragment_is_dropped():
    """`#jobDetails` is a scroll position on the page already being linked."""
    assert clean_link("https://acme.com/job/1#jobDetails") == "https://acme.com/job/1"


def test_a_clean_url_is_left_exactly_alone():
    """Arm and Iterable store URLs with no query at all. A cleaner that reshapes them — adds a
    trailing slash, drops the empty query differently — would churn every link for nothing."""
    for url in ("https://careers.arm.com/job/austin/project-manager/33099/98187464416",
                "https://job-boards.greenhouse.io/iterable/jobs/7984113"):
        assert clean_link(url) == url


# ── what must SURVIVE ───────────────────────────────────────────────────────

def test_the_job_id_survives():
    """`gh_jid` IS the posting on a company's own careers page. Stripping it yields Avathon's
    careers INDEX — a link that resolves, looks fine, and shows the wrong page. That is the
    failure mode a blocklist is chosen to avoid, and an allowlist would cause by default."""
    assert clean_link("https://avathongov.com/careers-job-listings/?gh_jid=4683241005") == \
        "https://avathongov.com/careers-job-listings/?gh_jid=4683241005"


@pytest.mark.parametrize("param", sorted(KEPT_ON_PURPOSE))
def test_every_param_kept_on_purpose_really_survives(param):
    """`KEPT_ON_PURPOSE` is a constant rather than a comment precisely so this loop can exist —
    the next person to look at a long URL will reach for the blocklist, and a comment saying
    "don't" is not checkable."""
    assert clean_link(f"https://acme.com/job/1?{param}=42") == \
        f"https://acme.com/job/1?{param}=42"


def test_an_unknown_parameter_is_kept():
    """The blocklist direction, stated as a property. Peak6's `group=1767` and EY's
    `feedId=353401` are unproven either way; an unproven parameter keeps its place because
    dropping one wrongly breaks the link while keeping one is merely untidy."""
    assert clean_link("https://careers.peak6.com/jobs/x/JR104975?group=1767") == \
        "https://careers.peak6.com/jobs/x/JR104975?group=1767"


def test_the_path_is_never_rewritten():
    """The explicit non-goal. `/application` renders the posting above the form: imperfect and
    alive. Trimming it to guess at a parent URL is how §Lessons 32 happened."""
    for url in ("https://jobs.ashbyhq.com/saronic/f33184f4/application",
                "https://expedia.wd108.myworkdayjobs.com/search/job/Austin/Senior-AI-Engineer/apply"):
        assert clean_link(url) == url


# ── unwrapping a redirect ───────────────────────────────────────────────────

def test_a_recruitics_redirect_unwraps_to_the_real_posting():
    """434 characters of ad redirect wrapping a metacareers.com posting. Sending the wrapper to
    a Meta recruiter is the single worst link in the table."""
    dirty = ("https://jsv3.recruitics.com/redirect?rx_cid=3239&rx_jobId=a1KiE0000000GrBUAU"
             "&rx_url=https%3A%2F%2Fwww.metacareers.com%2Fjobs%2F1738148070833455%2F")
    assert clean_link(dirty) == "https://www.metacareers.com/jobs/1738148070833455/"


def test_the_unwrapped_url_is_cleaned_too():
    """The destination carries its OWN tracking — `rx_ch`, `rx_id`, `rx_job` survived unwrapping
    when the blocklist named the wrapper's parameters one by one. Hence a `rx_` PREFIX rule.
    Cleaning the outside and leaving the inside is §Lessons 49's shape."""
    dirty = ("https://jsv3.recruitics.com/redirect?rx_cid=3239&rx_url="
             "https%3A%2F%2Fwww.metacareers.com%2Fjobs%2F17381%2F%3Frx_ch%3Dconnector%26rx_id%3Dcd50")
    assert clean_link(dirty) == "https://www.metacareers.com/jobs/17381/"


def test_a_relative_redirect_target_is_not_followed():
    """A posting that happens to carry `?url=/somewhere` must be left alone, not replaced by a
    fragment of itself. Only an ABSOLUTE destination is ever followed."""
    assert clean_link("https://acme.com/job/1?url=%2Frelative%2Fpath") == \
        "https://acme.com/job/1?url=%2Frelative%2Fpath"


def test_unwrapping_is_bounded():
    """A wrapper wrapping itself must terminate rather than recurse forever."""
    from urllib.parse import quote
    url = "https://acme.com/job/1"
    for _ in range(8):
        url = "https://r.test/go?redirect=" + quote(url, safe="")
    assert clean_link(url).startswith("https://")
