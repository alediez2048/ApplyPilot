"""A cover letter must never be addressed to a job board, an ATS vendor, or a made-up name.

Six real applications went out before any of this existed:

    Google                      "Dear Uploaded Hiring Team,"      applied 2026-08-03
    Yahoo                       "Dear Ouryahoo Hiring Team,"      applied 2026-08-03
    CoStar                      "Dear Costargroup Hiring Team,"   applied 2026-08-04
    Texas Children's Hospital   "Dear Oraclecloud Hiring Team,"   applied 2026-08-05
    LegalZoom                   "Dear Jobvite Hiring Team,"       applied 2026-08-08
    Q2                          "Dear Q2ebanking Hiring Team,"    applied 2026-08-10

Two causes, and the second is why nothing caught it for a month.

**The letter was generated from `job['site']`** — the DISCOVERY SOURCE, which is "Jobvite",
"Recruitics", "Workday", or the literal "Uploaded" this app invented when it could not tell. The
employer-resolution work fixed contact discovery and the dashboard's Company column and never
followed the same value into the documents, which is §Lessons 49 with the most expensive call
site left out.

**The validator could not catch it.** `validate_cover_letter` already required the letter to NAME
the employer, as an ERROR, and it passed every time — because it was handed the same wrong string
that wrote the letter, and the letter did name it. §Lessons 12, and the reason the guard here
reads the salutation OUT OF THE LETTER and takes nothing from the caller.

And `_infer_company("not-a-url") == "Uploaded"` was **asserted by a passing test**, so the
placeholder was not merely present, it was protected.
"""

from __future__ import annotations

import pytest

from applypilot.scoring.validator import (
    _is_usable_employer,
    _salutation_name,
    validate_cover_letter,
)

BODY = ("I have spent a decade building enterprise platforms and the last few years on "
        "production AI systems, which is why this role is interesting to me.\n\n"
        "Happy to walk through any of this in more detail.\n\nAlejandro")


def _letter(salutation: str) -> str:
    return f"{salutation}\n\n{BODY}"


# ── the six that shipped ────────────────────────────────────────────────────

#: Every one of these is a real salutation from `~/.applypilot/cover_letters/`, paired with the
#: employer the posting actually names. Four are TENANT SLUGS, which is why a blocklist could
#: never have been the answer: no list contains "Ouryahoo".
SHIPPED = [
    ("Uploaded",      "",                          "Google"),      # invented by _infer_company
    ("Ouryahoo",      "Yahoo",                     "Yahoo"),       # Workday tenant slug
    ("Costargroup",   "CoStar",                    "CoStar"),
    ("Oraclecloud",   "Texas Children's Hospital", "Texas Children's Hospital"),
    ("Jobvite",       "LegalZoom",                 "LegalZoom"),   # the ATS vendor
    ("Q2ebanking",    "Q2",                        "Q2"),          # Workday tenant slug
    ("Recruitics",    "Meta",                      "Meta"),        # ad distributor, not sent
]


@pytest.mark.parametrize("addressee,resolved,employer", SHIPPED,
                         ids=[s[2] for s in SHIPPED])
def test_every_salutation_that_really_shipped_is_now_an_error(addressee, resolved, employer):
    result = validate_cover_letter(_letter(f"Dear {addressee} Hiring Team,"), company=resolved)
    assert not result["passed"], f"{employer} would ship as 'Dear {addressee}'"
    assert any(addressee in e for e in result["errors"]), result


@pytest.mark.parametrize("addressee,resolved,employer", SHIPPED,
                         ids=[s[2] for s in SHIPPED])
def test_the_right_name_passes_on_every_one_of_them(addressee, resolved, employer):
    """Guard the guard: a check that failed everything would satisfy the test above. The letter
    each of these SHOULD have been is still valid."""
    if not resolved:
        pytest.skip("no employer resolvable for this row — covered by the generic-salutation test")
    assert validate_cover_letter(_letter(f"Dear {resolved} Hiring Team,"),
                                 company=resolved)["passed"]


def test_it_fails_even_when_the_caller_passes_the_same_wrong_name():
    """The §Lessons 12 case. The old check was handed `company="Uploaded"`, found "Uploaded" in
    the letter, and passed — a validator whose input is derived from its own demand cannot fail.
    A placeholder is not an employer whatever the caller believes."""
    result = validate_cover_letter(_letter("Dear Uploaded Hiring Team,"), company="Uploaded")
    assert not result["passed"]


def test_a_tenant_slug_is_caught_without_being_on_any_list():
    """The reason the rule is positive rather than a blocklist. "Ouryahoo" appears in no list in
    this codebase and never will; it fails because it is not the employer the posting names."""
    from applypilot.networking import derive
    assert "ouryahoo" not in derive._BOARD_NAMES
    assert not validate_cover_letter(_letter("Dear Ouryahoo Hiring Team,"),
                                     company="Yahoo")["passed"]


def test_a_real_employer_still_passes():
    assert validate_cover_letter(_letter("Dear Stripe Hiring Team,"), company="Stripe")["passed"]


@pytest.mark.parametrize("salutation", [
    "Dear Hiring Manager,",
    "Dear Hiring Team,",
    "Dear Recruiting Team,",
])
def test_the_generic_salutation_is_fine(salutation):
    """The correct fallback when no employer is known, and what twelve existing letters already
    use. Blocking it would leave nothing sayable at all."""
    assert validate_cover_letter(_letter(salutation))["passed"]


def test_an_unknown_employer_no_longer_demands_to_be_named():
    """Handing `company=""` must not resurrect the old error. Demanding that a letter name
    nothing is meaningless; demanding it name a placeholder is how this went wrong."""
    assert validate_cover_letter(_letter("Dear Hiring Manager,"), company="")["passed"]
    assert validate_cover_letter(_letter("Dear Hiring Manager,"), company="Uploaded")["passed"]


# ── the primitives ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,want", [
    ("Dear Acme Hiring Team,\n\nBody", "Acme"),
    ("Dear Acme Team,\n\nBody", "Acme"),
    ("Dear Acme,\n\nBody", "Acme"),
    ("Dear Texas Children's Hospital Hiring Team,\n\nBody", "Texas Children's Hospital"),
    # Names no company, so there is nothing to judge.
    ("Dear Hiring Manager,\n\nBody", ""),
    ("Dear Hiring Team,\n\nBody", ""),
    ("No salutation at all", ""),
])
def test_the_addressee_is_read_out_of_the_letter(text, want):
    assert _salutation_name(text) == want


def test_the_board_list_is_shared_not_copied():
    """`derive._BOARD_NAMES` is where the vendors this codebase HAS been burned by live, and it
    is reused rather than restated so a new one is learned about in both places (§Lessons 49).

    It is the secondary guard, not the primary one — note that `jobvite` is absent from it, and
    a LegalZoom application still went out saying "Dear Jobvite". That is exactly why the check
    above compares against the resolved employer instead of consulting a list."""
    from applypilot.networking import derive
    for vendor in ("recruitics", "oraclecloud", "greenhouse", "workday"):
        assert vendor in derive._BOARD_NAMES
        assert not _is_usable_employer(vendor)


def test_a_real_company_is_usable():
    for name in ("Stripe", "Texas Children's Hospital", "Q2", "LegalZoom", "Meta"):
        assert _is_usable_employer(name)


# ── the generator no longer reads `site` ────────────────────────────────────

def test_the_generator_resolves_the_employer_instead_of_reading_site():
    """`site` is the discovery source. Reading it is the direct cause of all six."""
    import inspect

    from applypilot.scoring import cover_letter
    src = inspect.getsource(cover_letter.generate_cover_letter)
    assert "job['site']" not in src and 'job["site"]' not in src, (
        "the cover letter is being built from the discovery source again")
    assert "_employer_for_letter" in src


def test_an_unknown_employer_reaches_the_prompt_as_a_refusal_not_a_blank():
    """A model handed "COMPANY:" followed by nothing invents one, which is the failure this is
    replacing rather than a new risk."""
    import inspect

    from applypilot.scoring import cover_letter
    src = inspect.getsource(cover_letter.generate_cover_letter)
    assert "Dear Hiring Manager" in src
    assert "do NOT name or" in src


def test_the_resolver_refuses_a_placeholder():
    """`_employer_for_letter` falls back to `job['company']`, and that column still holds
    "Uploaded" on a live row — so the fallback needs the same guard as everything else."""
    from applypilot.scoring.cover_letter import _employer_for_letter
    assert _employer_for_letter({"url": "https://www.linkedin.com/jobs/view/444",
                                 "company": "Uploaded", "site": "Uploaded"}) == ""
    assert _employer_for_letter({"url": "https://jobs.jobvite.com/legalzoom/job/x",
                                 "company": "Jobvite", "site": "Jobvite",
                                 "full_description": "About LegalZoom. LegalZoom is on a "
                                                     "mission. LegalZoom hires."}) == "LegalZoom"


# ── the two failures need different messages ────────────────────────────────

def test_an_unresolved_employer_and_a_wrong_one_read_differently():
    """Both block, and the operator has to do different things about them.

    "no employer could be resolved" means fix resolution — the row's URL carries no employer.
    "addressed to X, employer is Y" means the letter named the ATS instead of the company. A
    single message for both sends you looking in the wrong place, and mutation showed the
    branches were interchangeable because every test only asserted that the name appeared.
    """
    unresolved = validate_cover_letter(_letter("Dear Uploaded Hiring Team,"), company="")
    mismatch = validate_cover_letter(_letter("Dear Jobvite Hiring Team,"), company="LegalZoom")
    assert "no employer could be resolved" in unresolved["errors"][0]
    assert "but the employer is" in mismatch["errors"][0]


# ── §Lessons 1, in the comparison itself ────────────────────────────────────

@pytest.mark.parametrize("addressee,company", [
    ("Arm", "Armanino"),          # the original six-false-positive case
    ("Meta", "Metamorphic Labs"),
    ("Q2", "Q2ebanking"),         # a tenant slug CONTAINS the company; still not the company
])
def test_the_spelling_tolerance_is_not_a_substring_test(addressee, company):
    """`_same_employer` accepts "Scale AI" for "Scaleai" by comparing the alphanumerics as WHOLE
    strings. Relaxing that to `in` re-creates the bug this codebase has paid for four times, and
    it would do so inside the guard written to stop a wrong name reaching a recruiter."""
    from applypilot.scoring.validator import _same_employer
    assert not _same_employer(addressee, company)


def test_the_spelling_tolerance_still_does_its_job():
    """Guard the guard: refusing everything would satisfy the test above."""
    from applypilot.scoring.validator import _same_employer
    assert _same_employer("Scale AI", "Scaleai")
    assert _same_employer("CoStar", "costar")


def test_a_placeholder_is_rejected_on_its_own_merits():
    """`uploaded` happens to be in `_BOARD_NAMES` too, so removing the placeholder set changed
    nothing detectable — two guards agreeing looks like one guard working. These are placeholders
    and NOT board names, so they isolate the set."""
    from applypilot.networking import derive
    for name in ("Untitled", "Community", "External", "TBD"):
        assert name.lower() not in derive._BOARD_NAMES
        assert not _is_usable_employer(name)


# ── the prompt, behaviourally ───────────────────────────────────────────────

def test_an_unknown_employer_produces_a_prompt_that_refuses_to_name_one():
    """Asserted by RUNNING the builder, not by grepping the source for the sentence. The grep
    version passed against a mutation that made the branch unreachable — §Lessons 48, grep proves
    where a string is, not what the code does."""
    seen = {}

    class _Client:
        def chat(self, messages, **_):
            seen["user"] = messages[1]["content"]
            return "Dear Hiring Manager,\n\nBody.\n\nAlejandro"

    from applypilot.scoring import cover_letter
    real = cover_letter.get_client
    cover_letter.get_client = lambda *_a, **_k: _Client()
    try:
        cover_letter.generate_cover_letter(
            "RESUME",
            {"url": "https://www.linkedin.com/jobs/view/444", "title": "Engineer",
             "company": "Uploaded", "site": "Uploaded", "full_description": "Build things."},
            {"personal": {"full_name": "Alejandro"}})
    finally:
        cover_letter.get_client = real
    assert "COMPANY: not known" in seen["user"]
    assert "Uploaded" not in seen["user"], "the placeholder reached the prompt"


def test_a_known_employer_reaches_the_prompt_by_name():
    """Guard the guard: refusing always would pass the test above and break every letter."""
    seen = {}

    class _Client:
        def chat(self, messages, **_):
            seen["user"] = messages[1]["content"]
            return "Dear Stripe Hiring Team,\n\nStripe builds payments.\n\nAlejandro"

    from applypilot.scoring import cover_letter
    real = cover_letter.get_client
    cover_letter.get_client = lambda *_a, **_k: _Client()
    try:
        cover_letter.generate_cover_letter(
            "RESUME",
            {"url": "https://stripe.com/jobs/listing/x", "title": "Engineer",
             "company": "Stripe", "site": "Stripe", "full_description": "Stripe builds."},
            {"personal": {"full_name": "Alejandro"}})
    finally:
        cover_letter.get_client = real
    assert "COMPANY: Stripe" in seen["user"]


# ── the import, where the placeholder was born ──────────────────────────────

def test_importing_an_unresolvable_url_stores_no_company(tmp_path, monkeypatch):
    """The origin of all six. `_infer_company` returned "Uploaded" and the handler wrote it into
    BOTH `company` and `site` — and `site` is what the cover letter addressed itself to.

    `company` must be empty when the employer is unknown, and `site` must carry the SOURCE. They
    are different facts and collapsing them is what let a guess become a salutation.
    """
    import applypilot.database as database
    from applypilot import web_dashboard as wd

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)

    wd._import_urls("https://www.linkedin.com/jobs/view/4449383611/")
    row = database.get_connection(path).execute(
        "SELECT company, site, title FROM jobs WHERE url LIKE '%4449383611%'").fetchone()
    assert row is not None, "the import did not store the row"
    assert (row["company"] or "") == "", f"a placeholder employer was stored: {row['company']!r}"
    assert (row["site"] or ""), "the discovery source was lost as well"
    assert "uploaded job" not in (row["title"] or "").lower()


def test_importing_a_resolvable_url_still_stores_the_employer(tmp_path, monkeypatch):
    """Guard the guard: storing "" for everything would satisfy the test above."""
    import applypilot.database as database
    from applypilot import web_dashboard as wd

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)

    wd._import_urls("https://job-boards.greenhouse.io/affirm/jobs/7778204003")
    row = database.get_connection(path).execute(
        "SELECT company FROM jobs WHERE url LIKE '%7778204003%'").fetchone()
    assert (row["company"] or "").lower() == "affirm"
