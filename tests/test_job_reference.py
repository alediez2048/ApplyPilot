"""Naming the job in an email: the role's title, and a requisition number where it helps.

This replaced sending the posting URL. That version worked and read badly — 141 characters of
Workday link inline in an opening sentence is the single clearest "assembled by a tool" tell
after the em dash, and it was reverted on sight of the drafts.

Both halves were measured on the 33 live rows first, and the surprise was which one carried the
work:

    requisition   8 of 33 recoverable, none of them present in the posting TEXT
    title        11 of 33 UNUSABLE

The title is the job. Quoting a bad one is worse than naming no role at all — "I applied for the
Webai uploaded job role" cannot be recovered from in a first sentence, and §Lessons 42 caught
exactly that shape reaching a live draft as "the Betterup uploaded job". So an unusable title
resolves to "" and the prompt is told to say nothing rather than to invent something.
"""

from __future__ import annotations

import pytest

from applypilot.domain.jobref import clean_title, posting_reference, requisition
from applypilot.networking import outreach


# ── the title, which is the hard half ───────────────────────────────────────

@pytest.mark.parametrize("stored,company,want", [
    # Five live rows carry the scraper's placeholder. `collect_detail_intelligence` backfilled
    # 17 of 22 and the rest are expired postings and auth walls, so this is permanent.
    ("Webai uploaded job", "Webai", ""),
    ("Costargroup uploaded job", "CoStar", ""),
    # A careers INDEX page's <title>. Not a role, and it names the company twice over.
    ("LegalZoom Careers", "LegalZoom", ""),
    # The employer prefixed onto its own job title. The email already names the company.
    ("Salesforce - Forward Deployed Engineer (All Levels)", "Salesforce",
     "Forward Deployed Engineer (All Levels)"),
    ("Wander - Forward Deployed Engineer", "Wander", "Forward Deployed Engineer"),
    ("Senior AI Engineer at Acme", "Acme", "Senior AI Engineer"),
    # An HTML entity that reached the database undecoded and would have reached an inbox.
    ("Program Manager, Customer &amp; Community Engagement", "Sailpoint",
     "Program Manager, Customer & Community Engagement"),
    # Already clean: must pass through untouched.
    ("AI Solutions Engineer", "Affirm", "AI Solutions Engineer"),
    ("Senior IT Engineer (AI)", "Iterable", "Senior IT Engineer (AI)"),
])
def test_titles_from_the_live_board(stored, company, want):
    assert clean_title(stored, company) == want


def test_an_aggregator_sentence_is_not_a_title():
    """"Google hiring AI Sales Specialist…" is a BOARD's sentence about the posting.

    Stripped independently of the resolved employer, and that is not incidental: the live row
    with this shape has `company` stored as **"Uploaded"**, because its URL is a
    `linkedin.com/jobs/view/…` from which no employer is recoverable at all. A rule needing the
    company name could not have fired on the one row that has the problem.
    """
    got = clean_title("Google hiring AI Sales Specialist, Startups, Google Cloud in Austin, TX",
                      "Uploaded")
    assert got.startswith("AI Sales Specialist")
    assert "hiring" not in got


def test_the_company_name_is_not_eaten_out_of_a_role():
    """§Lessons 1, in the function that strips a company from a title. A company literally called
    "AI" must not turn "Senior AI Engineer" into "Senior Engineer" — the strip is anchored and
    separator-bound, never a substring replace."""
    assert clean_title("Senior AI Engineer", "AI") == "Senior AI Engineer"
    assert clean_title("AI Solutions Engineer", "AI") == "AI Solutions Engineer"


def test_a_title_that_is_only_the_company_says_nothing():
    assert clean_title("Acme", "Acme") == ""


def test_no_title_is_empty_not_a_guess():
    assert clean_title("", "Acme") == ""
    assert clean_title(None, "Acme") == ""


# ── the requisition ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,want", [
    ("https://salesforce.wd12.myworkdayjobs.com/x/job/CA/Forward-Deployed_JR349466", "JR349466"),
    ("https://q2ebanking.wd5.myworkdayjobs.com/Q2/job/Austin-TX/ML-Engineer_REQ-12289", "REQ-12289"),
    ("https://visa.wd5.myworkdayjobs.com/en-US/Visa/job/Austin/Sr-TPM_REF085286W", "REF085286W"),
    ("https://careers.expediagroup.com/job/senior-ai-engineer/austin-tx/R-108281/", "R-108281"),
    ("https://careers.peak6.com/jobs/technology/austin/solutions-engineer/JR104975", "JR104975"),
])
def test_real_requisition_numbers_are_found(url, want):
    assert requisition({"url": url}) == want


def test_an_underscore_does_not_hide_a_requisition():
    """`_` is a word character, so `\\b` does not match between `..._` and `REQ`. Two live rows
    went missing to that — Q2's `_REQ-12289` and Visa's `_REF085286W` — and a Workday URL puts an
    underscore there every single time. Character-class boundaries, not `\\b`."""
    assert requisition({"url": "https://x.example/job/Machine-Learning-Engineer_REQ-12289"})


@pytest.mark.parametrize("url", [
    "https://job-boards.greenhouse.io/affirm/jobs/7778204003",      # Greenhouse internal id
    "https://careers.costargroup.com/careers/job/446718602839",     # a bare number
    "https://jobs.ashbyhq.com/betterup/544ff316-842a-40a5-beb9",    # an Ashby uuid
])
def test_an_internal_id_is_not_a_requisition(url):
    """A recruiter would not recognise any of these. Quoting a machine id at somebody is the
    exact quality this whole change exists to remove, so the pattern requires a letter prefix a
    human sees printed on the posting."""
    assert requisition({"url": url}) == ""


def test_the_query_string_is_not_searched():
    """Tracking and session junk lives there; a match would be a coincidence, not an id."""
    assert requisition({"url": "https://x.example/job/1?ref=JR999999"}) == ""


def test_a_target_row_has_no_requisition():
    assert requisition({"url": "target:partnerships:ridgeline"}) == ""


def test_a_targets_space_names_no_posting():
    ref = posting_reference({"url": "target:x:y", "title": "Ridgeline"}, "pipeline/targets")
    assert ref == {"title": "", "req": ""}


# ── what the prompt then says ───────────────────────────────────────────────

RECRUITER = {"id": "c1", "full_name": "Sam Reed", "title": "Technical Recruiter",
             "company": "Acme", "email": "s@acme.test"}
PEER = {"id": "c2", "full_name": "Dana Whitfield", "title": "Engineering Manager",
        "company": "Acme", "email": "d@acme.test"}


def test_the_role_is_named_and_the_url_is_gone():
    block = outreach._posting_ref_block({"title": "Solutions Engineer", "req": ""}, PEER)
    assert "Solutions Engineer" in block
    assert "http" not in block, "a URL is back in the prompt"


def test_a_recruiter_is_offered_the_requisition():
    """It is how they find the application in their own ATS."""
    block = outreach._posting_ref_block({"title": "Solutions Engineer", "req": "JR104975"},
                                        RECRUITER)
    assert "JR104975" in block


def test_a_peer_is_told_NOT_to_mention_it():
    """A requisition number means nothing to an engineer and reads as machine-generated, which is
    the quality this change exists to fix. `rank.is_recruiter` decides, reused rather than
    re-implemented (§Lessons 49)."""
    block = outreach._posting_ref_block({"title": "Solutions Engineer", "req": "JR104975"}, PEER)
    assert "Do NOT mention it" in block
    assert "JR104975" not in block.split("Do NOT mention it")[0]


def test_an_unreadable_title_forbids_inventing_one():
    """The common case — six live rows. A model handed "ROLE:" followed by nothing invents one,
    and an invented job title in the first sentence of an application email is unrecoverable."""
    block = outreach._posting_ref_block({"title": "", "req": ""}, PEER)
    assert "never invent a title" in block.lower()


def test_an_unreadable_title_still_lets_the_posting_speak():
    """Found by generating, not by reading. With `LegalZoom Careers` as the stored title the live
    model wrote "the AI UX Designer role" — and the POSTING says exactly that, so it recovered
    the real name rather than inventing one. That is strictly better than staying vague, and the
    first wording ("refer to it in general terms and NEVER invent a job title") read as
    forbidding it. Reading a name from the description is not inventing it."""
    block = outreach._posting_ref_block({"title": "", "req": ""}, PEER)
    assert "use that name" in block.lower()


def test_an_unreadable_title_still_reaches_the_model_as_a_block():
    """Guard the guard: returning "" for an empty title would leave the prompt with no ROLE
    section at all, which is the state that invents one. It must speak up."""
    assert outreach._posting_ref_block({"title": "", "req": ""}, PEER).strip()


def test_the_brief_form_keeps_the_role_and_drops_the_guidance():
    """Follow-ups get the short version. It may shorten the GUIDANCE and must never drop the
    FIELD — a channel that silently loses a layer is the bug CTX-3 exists to close."""
    brief = outreach._posting_ref_block({"title": "Solutions Engineer", "req": "JR104975"},
                                        RECRUITER, brief=True)
    full = outreach._posting_ref_block({"title": "Solutions Engineer", "req": "JR104975"},
                                       RECRUITER)
    assert "Solutions Engineer" in brief and "JR104975" in brief
    assert len(brief) < len(full)


# ── the guarantee ───────────────────────────────────────────────────────────

def test_the_requisition_is_spliced_after_the_role():
    """Measured before building: with the prompt asking for it, four live drafts to a recruiter
    carried the number TWICE. A coin flip is not "include it" (§Lessons 9, 12)."""
    body = "Hi Sam,\n\nI just applied for the Solutions Engineer role at Q2.\n\nThanks,\nAlejandro"
    out = outreach.ensure_requisition(body, "Solutions Engineer", "JR104975")
    assert "Solutions Engineer (JR104975) role at Q2" in out


def test_it_never_appends_a_dangling_number():
    """The one way this differs from `ensure_intro_deck`, and the reason it is not modelled on it
    exactly. A deck link is an offer that stands alone; a requisition is a QUALIFIER on a role,
    and a line reading "REQ-12289" under the sign-off is the machine-assembled footer this whole
    change removed. No role mention in the body means no insertion at all."""
    body = "Hi Sam,\n\nI applied to your team recently.\n\nThanks,\nAlejandro"
    assert outreach.ensure_requisition(body, "Solutions Engineer", "JR104975") == body


def test_it_is_idempotent():
    body = "I applied for the Solutions Engineer (JR104975) role."
    assert outreach.ensure_requisition(body, "Solutions Engineer", "JR104975") == body


def test_a_number_the_model_placed_elsewhere_is_left_alone():
    """It may have written "requisition JR104975" in a closing line, which is also fine. The
    guarantee is that the number is PRESENT, not that it sits in one spot."""
    body = "I applied for the Solutions Engineer role. The req is JR104975 if that helps."
    assert outreach.ensure_requisition(body, "Solutions Engineer", "JR104975") == body


def test_it_preserves_the_models_own_capitalisation():
    """Matched case-insensitively and spliced by index, so a model that wrote "solutions
    engineer" keeps its own words rather than having the stored title pasted over them."""
    body = "I applied for the solutions engineer role."
    out = outreach.ensure_requisition(body, "Solutions Engineer", "JR104975")
    assert "solutions engineer (JR104975) role" in out


def test_no_requisition_leaves_the_body_untouched():
    body = "Hi Sam,\n\nI applied for the Solutions Engineer role.\n\nThanks,\nAlejandro"
    assert outreach.ensure_requisition(body, "Solutions Engineer", "") == body


def test_one_predicate_decides_who_gets_it():
    """The prompt block and the guarantee must agree. Two implementations of "is this person a
    recruiter" would let the prompt ask for a number the guarantee refuses to insert, or the
    reverse — §Lessons 49."""
    ref = {"title": "Solutions Engineer", "req": "JR104975"}
    assert outreach._wants_requisition(RECRUITER, ref) is True
    assert outreach._wants_requisition(PEER, ref) is False
    assert outreach._wants_requisition(RECRUITER, {"title": "X", "req": ""}) is False
