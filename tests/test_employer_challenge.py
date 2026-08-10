"""The employer the posting actually talks about, versus the one the URL implies.

Six times this codebase has resolved an employer to an ATS or ad vendor and emailed that
vendor's staff: **Ats** (Rippling), **Hr** (Breezy), **Edu** (Stanford), **Ouryahoo** (Workday
tenant), **Oraclecloud** (Texas Children's), **Recruitics** (Meta). Every fix was the same shape
— add the vendor to a blocklist — which only ever protects against a vendor somebody has already
been burned by. Then `jobs.jobvite.com/legalzoom/...` arrived and resolved to "Jobvite", and
three contacts at `@jobvite.com` were stored with `confidence: high` against a LegalZoom role.

The general rule needs no list: **the employer of a role is named in the posting for that role.**

    "Jobvite"  appears 0 times in its own posting
    "LegalZoom" appears 7, and the posting opens "About LegalZoom"

Measured across all 32 live rows before it was written. Three rows flip (LegalZoom, Q2, Apex
Fintech Solutions), twenty-nine do not, and there are no false positives — including nine rows
whose resolved name appears zero times and is nonetheless correct, because nothing corroborated
challenges them.

The safety comes from both halves being corroboration:
  * a name the posting DOES mention is never challenged;
  * a challenger must be named `_MIN_CHALLENGER_MENTIONS` times, as a whole word.
"""

from __future__ import annotations

import pytest

from applypilot.networking import derive

LEGALZOOM = ("About LegalZoom. LegalZoom is on a mission to help people navigate the legal "
             "system with confidence. LegalZoom was founded in 2001, and today LegalZoom serves "
             "millions. Working at LegalZoom means owning your work. LegalZoom is hiring.")


def _job(**kw):
    base = {"url": "", "application_url": "", "company": "", "site": "", "full_description": ""}
    base.update(kw)
    return base


# ── the case that prompted it ───────────────────────────────────────────────

def test_jobvite_resolves_to_legalzoom():
    job = _job(url="https://jobs.jobvite.com/legalzoom/job/oWLtAfwu",
               company="Jobvite", site="Jobvite", full_description=LEGALZOOM)
    assert derive.derive_company(job) == "LegalZoom"


def test_it_wins_against_a_STORED_company_name():
    """The important half, and the reason a blocklist was needed before.

    `company` is written AT IMPORT from the hostname, and step 1 of the resolver trusts a stored
    value — so a hostname guess becomes a stored "fact" that outranks the posting forever. The
    challenger has to be able to overrule that, or every new vendor needs a blocklist entry
    before the description can ever be consulted.
    """
    job = _job(url="https://jobs.someats.example/legalzoom/job/x",
               company="Someats", site="Someats", full_description=LEGALZOOM)
    assert derive.derive_company(job) == "LegalZoom"


def test_a_vendor_nobody_has_ever_heard_of_is_handled():
    """The whole claim. This host appears in no list in this codebase, and a test asserts that
    below — the point is that being unknown costs nothing."""
    job = _job(url="https://careers.zorbtrack-hire.example/legalzoom/job/44",
               company="Zorbtrack-hire", full_description=LEGALZOOM)
    assert derive.derive_company(job) == "LegalZoom"


def test_that_vendor_really_is_unknown_to_the_codebase():
    """Guard the guard: the test above proves nothing if the name is quietly on a list. Same
    trap as the channel test that named SMS and silently broke when SMS shipped."""
    import pathlib
    src = pathlib.Path(derive.__file__).read_text(encoding="utf-8")
    assert "zorbtrack" not in src.lower()


# ── what must NOT move ──────────────────────────────────────────────────────

def test_a_corroborated_name_is_never_challenged():
    """The first safety half, and it needs a challenger that would otherwise WIN.

    The original version used Affirm on `job-boards.greenhouse.io/affirm/...`, where the only
    candidate is "affirm" itself — so deleting the corroboration guard entirely changed nothing
    and the mutation survived. §Lessons 1's shape for the third time this session: a test that
    passes because the thing it varies has no effect on the fixture.

    Here the incumbent is named twice and a DIFFERENT corroborated company is named nine times,
    which is a real shape — a posting hosted on a parent or partner domain that the copy also
    talks about. Without the guard this row silently becomes "Globex".
    """
    job = _job(url="https://globex.example/jobs/engineer", company="Acme",
               full_description=("Acme is hiring. At Acme we build. " + "Globex " * 9))
    assert derive.derive_company(job) == "Acme"


def test_the_most_mentioned_candidate_wins_not_the_first():
    """When BOTH slots name a real company, the posting's own emphasis breaks the tie.

    On today's corpus the first qualifying candidate is always also the best one, so first-wins
    and best-wins are indistinguishable — mutation found exactly that and it is the reason this
    test exists rather than an accident being preserved. The shape is real:
    `peak6group.wd1.myworkdayjobs.com/apexfintechsolutions/...` puts a parent in the host slot and
    the hiring subsidiary in the path slot, and only the counts say which one the role is at.
    """
    job = _job(url="https://globex.example/initech/job/1", company="Someats",
               full_description=("Globex " * 4) + ("Initech is hiring. " * 9))
    assert derive.derive_company(job) == "Initech"


def test_a_single_mention_is_not_enough():
    """A company named once in a posting is a partner, a customer or a competitor. Only the
    employer is named repeatedly."""
    job = _job(url="https://jobs.someats.example/acme/job/1", company="Someats",
               full_description="We integrate with Acme. " + "Other words. " * 40)
    assert derive.derive_company(job) != "Acme"


def test_a_deep_path_segment_is_never_a_candidate():
    """The first version of this allowed the first three path segments and came one coincidence
    away from renaming the live PEAK6 row to **"Technology"** — its path is
    /jobs/technology/austin-texas-united-states-of-america/solutions-engineer/JR104975, and an
    engineering posting says "technology" plenty of times. Only the host's first label and the
    FIRST path segment are tenant slots; everything deeper is location, discipline or an id.
    """
    job = _job(url="https://careers.acmeco.example/jobs/technology/austin/engineer/JR1",
               company="Acmeco",
               full_description="We work in technology. Technology is our technology. " * 6)
    assert derive.derive_company(job) != "Technology"


@pytest.mark.parametrize("segment", ["job", "jobs", "careers", "search", "en-US", "apply",
                                     "companies", "about", "hcmUI", "sites"])
def test_structural_url_furniture_is_never_a_company(segment):
    job = _job(url=f"https://x.example/{segment}/thing", company="X",
               full_description=(f"We post {segment} here. " * 8))
    assert (derive.derive_company(job) or "").lower() != segment.lower()


def test_a_requisition_id_is_never_a_company():
    job = _job(url="https://x.example/JR104975/detail", company="X",
               full_description="JR104975 " * 10)
    assert derive.derive_company(job) == "X"


def test_an_uncorroborated_incumbent_with_no_challenger_survives():
    """Nine live rows resolve to a name the posting never spells — Scale AI writes itself
    "Scale AI" against a `scaleai` slug, and Texas Children's Hospital does not introduce itself
    at all. Absence of corroboration is not evidence of error, so nothing moves."""
    job = _job(url="https://jobs.ashbyhq.com/scaleai/abc", company="Scaleai",
               full_description="Build the data engine. Ship models. " * 20)
    assert derive.derive_company(job) == "Scaleai"


# ── provenance ──────────────────────────────────────────────────────────────

def test_json_ld_outranks_both_corrections():
    """`hiringOrganization` is the employer stating its own name in machine-readable form. It is
    evidence, not an inference, and running the tenant-slug repair over it trimmed a legal suffix
    it mistook for an ATS affix: **"Acme Corp" became "Acme"**. Correcting a guess is the job;
    correcting a fact is damage. §Lessons 34's rule, applied to names instead of domains."""
    desc = 'x {"hiringOrganization": {"@type":"Organization","name":"Acme Corp"}} x'
    assert derive.derive_company({"full_description": desc, "site": "LinkedIn"}) == "Acme Corp"


# ── the primitive, directly ─────────────────────────────────────────────────

def test_the_challenger_returns_the_postings_own_spelling():
    """"legalzoom" is in the URL; "LegalZoom" is what a human wrote. The name goes into an email
    and into an Apollo query, so the capitalisation is not cosmetic."""
    job = _job(url="https://jobs.jobvite.com/legalzoom/job/x", full_description=LEGALZOOM)
    assert derive.challenge_company_from_path("Jobvite", job) == "LegalZoom"


def test_no_description_means_no_challenge():
    """At IMPORT there is no posting yet — enrichment has not run. The challenge must be a no-op
    rather than a guess, which is precisely why the stored name cannot be trusted later."""
    job = _job(url="https://jobs.jobvite.com/legalzoom/job/x")
    assert derive.challenge_company_from_path("Jobvite", job) is None


def test_it_does_not_rename_a_company_to_itself():
    job = _job(url="https://jobs.ashbyhq.com/zello/x", full_description="Zello " * 10)
    assert derive.challenge_company_from_path("Zello", job) is None


# ── the audit surface ───────────────────────────────────────────────────────

def test_the_audit_query_carries_everything_the_resolver_reads():
    """The audit's own SELECT is the thing most likely to lie, and it did.

    First version pulled `url, company, title, full_description` and silently missed the PEAK6
    row — its employer lives in the Workday tenant `apexfintechsolutions`, which is in
    `application_url` and nowhere else. An audit that under-reports reads exactly like a clean
    bill of health, so this is §Lessons 47 with the stakes raised: the tool built to find the
    bug had the bug.
    """
    import inspect

    from applypilot import cli
    src = inspect.getsource(cli._audit_employers)
    query = src[src.index("SELECT"):src.index("WHERE url NOT LIKE")]
    for column in ("url", "application_url", "company", "site", "full_description"):
        assert column in query, f"{column} missing from the audit query — the resolver reads it"


def test_the_audit_reports_how_each_name_was_reached():
    """`challenged` and `refined` need different responses and the output has to say which.

    A refined name is the same company spelled differently, so its contacts are fine. A
    challenged one is a DIFFERENT company, so every contact stored under it works somewhere else
    — which is the state that emailed four City of Atlanta employees (§Lessons 68). Collapsing
    the two into "wrong" loses the only part that tells the operator what to do.
    """
    import inspect

    from applypilot import cli
    src = inspect.getsource(cli._audit_employers)
    assert '"challenged"' in src, "the audit no longer distinguishes a wrong ENTITY from a wrong spelling"


# ── the column the operator actually reads ──────────────────────────────────

def test_the_dashboard_shows_the_employer_not_the_job_board():
    """The row's Company column was `site` — the DISCOVERY SOURCE.

    So a LegalZoom application displayed "Jobvite" and a Meta one displayed "Recruitics", which
    is what the operator saw every day while the resolver beside it was right. `contact_company`
    has carried the resolved employer the whole time and drives the connection counts, so the
    data was present and the label ignored it.

    Seven of thirty-two live rows disagreed. A wrong name here is not cosmetic: it is what the
    operator recognises the row by, and it is the string they read back when deciding whether a
    draft is aimed at the right company.
    """
    import inspect

    from applypilot import web_dashboard as wd
    src = inspect.getsource(wd)
    marker = '"company": row['
    i = src.index(marker)
    line = src[i:src.index("\n", i)]
    assert 'row["company"]' in line, f'the Company column reads {line.strip()} — site is the board'


def test_the_dashboard_query_selects_the_company_column():
    """Guard the other half. The payload can name `row["company"]` all it likes; if the SELECT
    never fetched it the row raises or reads blank. §Lessons 47, which is the third time this
    exact pair has been wrong in this file's neighbourhood."""
    import inspect

    from applypilot.repo import jobs as repo
    src = inspect.getsource(repo.dashboard_rows)
    select = src[src.index("SELECT"):src.index("FROM jobs")]
    assert "company" in select, "dashboard_rows stopped selecting `company`"
