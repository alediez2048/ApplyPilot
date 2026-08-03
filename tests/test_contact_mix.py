"""A recruiter and a colleague are different searches, not different scores.

Reported as "too many talent acquisition people". Measured against the live Apollo API on a real
Yahoo job, the blended query was:

    blended (role + recruiter titles) -> 25 candidates, 0 peers, 25 recruiters
    "AI Operations Strategist"        ->  0
    "Operations Strategist"           ->  0
    "Strategist"                      -> 25 peers
    recruiter titles only             -> 25 recruiters

Two independent failures, and the ranking stage was innocent of both. A bespoke multi-word title
matches nobody in Apollo, so the recruiter titles took every slot — and `select()` was then
choosing five recruiters out of a pool of five recruiters, doing exactly what it was told.

A single query cannot produce a mix, because the provider decides the composition. So the search
is split in two, each side gets a minimum, and the results are INTERLEAVED rather than scored
into one list.

The second failure was upstream of everything: the dashboard import writes
`title = f"{company} uploaded job"` for every pasted URL and nothing ever replaced it, so
broadening reached the bare word "job" and returned four people at Yahoo titled "Job",
"Student Job", "No job" and "Job Captain".
"""

from __future__ import annotations

import pytest

from applypilot.enrichment.detail import _clean_role_title, is_placeholder_title
from applypilot.networking import rank


def _p(name, title, key=None):
    return {"full_name": name, "title": title, "key": key or name}


# ── peer titles: widen until something exists ───────────────────────────────

def test_titles_widen_from_the_front():
    """English job titles put the qualifier first and the function last, so "Senior Technical
    Program Manager" is a Program Manager — not a Senior Technical."""
    assert rank.peer_titles("Senior Technical Program Manager") == [
        "Senior Technical Program Manager", "Technical Program Manager", "Program Manager"]


def test_the_exact_title_is_tried_first():
    """When an employer really does have someone with this title, they are the best possible
    match and no widening should happen."""
    assert rank.peer_titles("AI Operations Strategist")[0] == "AI Operations Strategist"


def test_it_widens_far_enough_to_find_anyone():
    """The measured case: the first two variants return nobody at Yahoo, the third returns 25."""
    assert "Strategist" in rank.peer_titles("AI Operations Strategist")


def test_decoration_after_a_separator_is_dropped():
    assert rank.peer_titles("Staff Software Engineer, Search")[0] == "Staff Software Engineer"
    assert rank.peer_titles("Product Manager (Remote)")[0] == "Product Manager"


def test_it_never_widens_down_to_a_bare_rank():
    """"Manager" is not a role, it is a level. Searching it returns an arbitrary slice of the
    company rather than anyone doing this job."""
    assert "Manager" not in rank.peer_titles("Senior Technical Program Manager")
    assert "Director" not in rank.peer_titles("Director of Data Science")


def test_a_placeholder_title_yields_no_search_terms():
    """The bug that produced colleagues called "Job" and "No job". A query for "job" is not a
    narrow search that happens to return the wrong people — it is a search with no subject."""
    assert rank.peer_titles("Uploaded uploaded job") == []
    for junk in ("job", "jobs", "career opening", "apply"):
        assert rank.peer_titles(junk) == [], junk


def test_recruiter_titles_are_never_peer_titles():
    """Blending the two is what produced 25 recruiters and 0 peers."""
    joined = " ".join(rank.peer_titles("Talent Acquisition Partner")).lower()
    assert rank.peer_titles("Software Engineer") and "recruiter" not in joined


# ── the mix ─────────────────────────────────────────────────────────────────

PEERS = [_p(f"peer{i}", "Strategist") for i in range(10)]
RECRUITERS = [_p(f"rec{i}", "Talent Acquisition") for i in range(10)]


def test_both_minimums_are_met():
    out = rank.select_mix(PEERS, RECRUITERS, "AI Operations Strategist",
                          min_peers=4, min_recruiters=4, n=8)
    sides = [c["side"] for c in out]
    assert sides.count("peer") >= 4 and sides.count("recruiter") >= 4


def test_the_two_sides_stay_interleaved_all_the_way_down():
    """Not cosmetic. The caller enriches down this list in batches and drops whoever fails
    verification, so front-loading four peers means a company whose first four peers all fail
    leaves an all-recruiter result — the original bug, one step later.
    """
    out = rank.select_mix(PEERS, RECRUITERS, "Strategist", min_peers=2, min_recruiters=2)
    sides = [c["side"] for c in out]
    first_half, second_half = sides[: len(sides) // 2], sides[len(sides) // 2:]
    for half in (first_half, second_half):
        assert "peer" in half and "recruiter" in half, sides


def test_a_short_side_does_not_shrink_the_result():
    """Quotas are minimums, not caps. A company with one colleague listed should still return
    everyone the provider had, not four people."""
    out = rank.select_mix(PEERS[:1], RECRUITERS, "Strategist", min_peers=4, min_recruiters=4, n=8)
    assert len(out) == 8
    assert sum(c["side"] == "peer" for c in out) == 1


def test_a_recruiter_in_the_peer_pool_is_still_a_recruiter():
    """"Talent Acquisition Strategist" answers a search for "Strategist". Classifying by which
    QUERY produced someone rather than by their title would quietly rebuild the imbalance."""
    peers = [_p("sneaky", "Talent Acquisition Strategist"), _p("real", "Strategist")]
    out = rank.select_mix(peers, RECRUITERS, "Strategist", min_peers=4, min_recruiters=4)
    assert [c["side"] for c in out if c["full_name"] == "sneaky"] == ["recruiter"]
    assert [c["side"] for c in out if c["full_name"] == "real"] == ["peer"]


def test_nobody_appears_twice():
    out = rank.select_mix(PEERS + PEERS, RECRUITERS, "Strategist")
    keys = [c["key"] for c in out]
    assert len(keys) == len(set(keys))


def test_empty_pools_do_not_hang():
    assert rank.select_mix([], [], "Strategist") == []
    assert len(rank.select_mix([], RECRUITERS, "Strategist", n=3)) == 3


# ── the role title, at its source ───────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("AI Specialist - SLAC Careers", "AI Specialist"),
    ("Senior Technical Program Manager | Visa", "Senior Technical Program Manager"),
    ("AI Operations Strategist", "AI Operations Strategist"),
])
def test_site_furniture_is_stripped_from_a_page_title(raw, expect):
    assert _clean_role_title(raw) == expect


@pytest.mark.parametrize("raw", [
    "Yahoo uploaded job",       # the import's own placeholder, read back
    "Careers",                  # not a role
    "Job Search - Workday",     # the board's index page
    "",
    None,
])
def test_a_title_that_is_not_a_role_is_refused(raw):
    """Refusing is what keeps the placeholder in place, which is honest. Overwriting it with
    "Job Search" would look like a real role everywhere downstream."""
    assert _clean_role_title(raw) == ""


def test_only_the_invented_placeholder_is_replaceable():
    """A title the operator or a discovery source set is theirs. Only the import's own
    `f"{company} uploaded job"` may be overwritten by a scrape."""
    assert is_placeholder_title("Yahoo uploaded job")
    assert not is_placeholder_title("AI Operations Strategist")
    assert not is_placeholder_title("Job Captain")


def test_the_scraper_persists_a_recovered_title_only_over_a_placeholder():
    """`collect_detail_intelligence` had always captured `page_title` and nothing read it."""
    import pathlib

    from applypilot.enrichment import detail
    src = pathlib.Path(detail.__file__).read_text(encoding="utf-8")
    assert 'intel.get("page_title")' in src, (
        "the page title is captured and still never used")
    block = src[src.index('role_title = result.get("role_title")'):]
    assert "is_placeholder_title(title)" in block[:400], (
        "a scraped title can overwrite one the operator set")


# ── everything the 21-job backfill turned up ────────────────────────────────

def test_the_company_prefix_is_not_mistaken_for_the_role():
    """"Salesforce - Forward Deployed Engineer" cut at the first separator and searched for
    "Salesforce" — a company name, which matches nobody by title and then widens to nothing.

    Keeping the LONGEST segment fixes it; ties go to the earlier one so a trailing
    specialisation ("Product Manager, Business Operations") does not displace the role.
    """
    assert rank.peer_titles("Salesforce - Forward Deployed Engineer (All Levels)")[0] == \
        "Forward Deployed Engineer"
    assert rank.peer_titles("Avathon Government - Senior AI Engineer")[0] == "Senior AI Engineer"
    assert rank.peer_titles("Product Manager, Business Operations")[0] == "Product Manager"
    assert rank.peer_titles("Field Engineer, Public Sector")[0] == "Field Engineer"


@pytest.mark.parametrize("raw,expect", [
    # Greenhouse titles every application page this way. The "starts with Job" rule threw away
    # five real roles before the actual titles were read instead of guessed at.
    ("Job Application for AI Solutions Engineer at Affirm", "AI Solutions Engineer"),
    ("Job Application for Senior IT Engineer (AI) at Iterable", "Senior IT Engineer (AI)"),
    ("Job Application for Field Engineer, Public Sector at Scale AI", "Field Engineer, Public Sector"),
    # The employer is not part of the role; leaving it on widens the peer search through it.
    ("Senior AI Automation Engineer @ BetterUp", "Senior AI Automation Engineer"),
    ("Product Engineer at Hamming AI", "Product Engineer"),
    ("Head of AI at Scale", "Head of AI"),
])
def test_real_page_titles_from_the_backfill(raw, expect):
    assert _clean_role_title(raw) == expect


@pytest.mark.parametrize("raw", [
    "About the role", "What you will do", "Responsibilities", "The Role", "Our Mission",
    "Apply Now", "Job not found", "No results found",
])
def test_a_section_heading_is_never_the_role(raw):
    """Only reachable because the heading fallback reads h2 — Workday renders an EMPTY h1 and
    puts the role in an h2, and every posting on earth also has an h2 saying "About the role"."""
    assert _clean_role_title(raw) == ""


def test_the_heading_fallback_is_wired_and_ordered_after_the_title():
    """The Visa posting titles itself "Create Account" behind its auth wall while still showing
    "Senior Technical Program Manager" on the page. Without the fallback that role is
    unrecoverable; with it ahead of the page title, a good <title> would be overridden by a
    heading like "Careers"."""
    import pathlib

    from applypilot.enrichment import detail
    src = pathlib.Path(detail.__file__).read_text(encoding="utf-8")
    assert '"h2"' in src, "the h2 fallback is gone; Workday roles become unrecoverable"
    assert ('result["role_title"] = (_clean_role_title(intel.get("page_title"))\n'
            '                            or _clean_role_title(intel.get("heading")))') in src, (
        "the heading is consulted before the page title, or not at all")
