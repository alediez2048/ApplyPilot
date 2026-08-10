"""The posting link that goes in every job-search email.

The ask was "every email should reference the job". The work was almost entirely in deciding
WHICH string to send, because the two candidate columns are both wrong most of the time.
Measured across the 32 live rows before writing the module:

    `application_url` is the FORM, not the posting, whatever its name says. Google's is the
    relative path `./apply?jobId=…` (unsendable), Peak6's and Expedia's are Workday `/apply`
    endpoints, Stanford's contains two `?` and is malformed.

    Nearly every `url` carries the aggregator we found it through: `utm_source=linkedin`,
    `gh_src=…`, `source=LinkedIn`, `__jvsd=LinkedIn`. Forwarding that to a recruiter announces
    the pipeline in the one message whose point is not reading like one.

The rule that falls out, and the one these tests exist to hold: **strip parameters and unwrap
redirects, never rewrite paths.** A dropped query param cannot change which page loads and an
unwrapped redirect resolves to its own destination — both are provably safe. Trimming a trailing
`/application` to "recover the posting" is a guess about a URL space we do not own, and §Lessons
32 is what that costs: four recruiters were sent a deck link that 404'd.
"""

from __future__ import annotations

import pytest

from applypilot.domain.joblink import KEPT_ON_PURPOSE, clean_link, posting_link
from applypilot.networking import outreach


# ── choosing the field ──────────────────────────────────────────────────────

def test_the_posting_wins_over_the_application_form():
    """`url` is the posting, `application_url` is the form. The names say the opposite, which is
    the entire reason this test is the first one in the file."""
    job = {"url": "https://jobs.ashbyhq.com/betterup/544ff316",
           "application_url": "https://jobs.ashbyhq.com/betterup/544ff316/application"}
    assert posting_link(job) == "https://jobs.ashbyhq.com/betterup/544ff316"


def test_the_form_is_used_when_there_is_no_posting():
    """A form link is still a page about the role. Nothing is not."""
    job = {"url": "", "application_url": "https://careers.acme.com/job/12"}
    assert posting_link(job) == "https://careers.acme.com/job/12"


def test_a_relative_application_url_is_refused():
    """Google stores `./apply?jobId=CiUAL2Fck…`. In a mail client that resolves against the
    reader's own host, which is to say nowhere — the single worst outcome, because it renders as
    a link and fails only on click."""
    assert posting_link({"url": "", "application_url": "./apply?jobId=CiUAL2Fck"}) == ""


def test_a_target_row_has_no_posting():
    """A targets Space pitches a company; the anchor is `target:<space>:<slug>` and there is no
    job. §Lessons 70 — the read filter is not the guard, the write path is."""
    assert posting_link({"url": "target:partnerships:ridgeline"}) == ""


def test_a_target_anchor_is_refused_even_with_an_application_url():
    """Belt and braces: the anchor decides, and a stray `application_url` on a targets row must
    not open a back door. Without the early return this falls through to the second field."""
    assert posting_link({"url": "target:partnerships:ridgeline",
                         "application_url": "https://ridgeline.com/careers/1"}) == ""


def test_no_urls_at_all_is_empty_not_an_error():
    assert posting_link({}) == ""
    assert posting_link(None) == ""


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


# ── the guarantee ───────────────────────────────────────────────────────────

LINK = "https://acme.com/job/1"


def test_a_dropped_link_is_appended():
    """A prompt instruction is not a guarantee (§Lessons 9, 12), and a mail with no link looks
    exactly like a mail with one until somebody goes looking."""
    body = "Hi Sarah,\n\nI applied for the role.\n\nThanks,\nAlejandro"
    out = outreach.ensure_job_link(body, LINK)
    assert LINK in out


def test_it_goes_above_the_signoff():
    """A link under "Thanks, / Alejandro" reads as a signature block. Same placement rule as the
    deck sentence, and the same reason."""
    body = "Hi Sarah,\n\nI applied for the role.\n\nThanks,\nAlejandro"
    lines = [ln for ln in outreach.ensure_job_link(body, LINK).splitlines() if ln.strip()]
    assert lines.index(next(ln for ln in lines if LINK in ln)) < lines.index("Thanks,")


def test_a_link_the_model_placed_itself_is_left_alone():
    """Idempotent, and it must not append a second copy next to a naturally-phrased one — which
    is precisely the repetition `ensure_intro_deck` caused when it ran unconditionally."""
    body = f"Hi Sarah,\n\nI applied for the Solutions Engineer role ({LINK}).\n\nThanks,\nAlejandro"
    assert outreach.ensure_job_link(body, LINK) == body
    assert outreach.ensure_job_link(outreach.ensure_job_link(body, LINK), LINK) == body


def test_running_it_twice_adds_one_link():
    once = outreach.ensure_job_link("Hi.\n\nThanks,\nAlejandro", LINK)
    assert outreach.ensure_job_link(once, LINK) == once
    assert once.count(LINK) == 1


def test_no_link_means_the_body_is_untouched():
    """The empty string is the "no link" signal from `posting_link`, and appending nothing must
    not append a dangling sentence."""
    body = "Hi Sarah,\n\nThanks,\nAlejandro"
    assert outreach.ensure_job_link(body, "") == body


def test_the_fallback_sentence_is_never_shown_to_the_model():
    """§Lessons 42: the SMS prompt's suggested phrasing came back in 5 of 5 drafts. This sentence
    only exists for bodies where the model wrote no link at all, so it must not appear in any
    prompt — otherwise it becomes the phrasing every recipient at one company receives."""
    block = outreach._job_link_block(LINK)
    assert outreach.JOB_LINK_SENTENCE.format(url=LINK) not in block
    assert outreach.JOB_LINK_SENTENCE.split("{")[0].strip() not in block


# ── which channels carry it ─────────────────────────────────────────────────
#
# The rule is not "everywhere". Two channels are excluded by rules that predate this work and
# both would be actively harmed: a text must never carry a URL (an unrecognised number sending a
# link is the strongest spam signal there is, and carriers filter on it), and a LinkedIn
# connect-note has a 300-character budget that LinkedIn penalises links inside.

import applypilot.domain.space as sp  # noqa: E402

JOB = {"url": "https://acme.com/job/1?utm_source=linkedin", "title": "Applied AI Engineer",
       "company": "Acme", "site": "Greenhouse", "full_description": "Build agent pipelines.",
       "space_id": "job-search"}
CONTACT = {"id": "c1", "full_name": "Sarah Chen", "title": "Head of Engineering",
           "company": "Acme", "email": "s@acme.test", "match_reason": "works at the company"}
PROFILE = {"personal": {"full_name": "Jorge Alejandro Diez", "preferred_name": "Alejandro"},
           "experience": {"target_role": "Applied AI Engineer"}}


def _capture(fn, *a, **kw) -> dict:
    """Run a drafter with the LLM stubbed and return the prompts it built."""
    seen = {}

    class _Client:
        def chat(self, messages, **_):
            seen["system"], seen["user"] = messages[0]["content"], messages[1]["content"]
            return ('{"subject":"s","body":"Hi.\\n\\nThanks,\\nAlejandro",'
                    '"linkedin_note":"n","message":"m"}')

    real = outreach.get_client
    outreach.get_client = lambda *_a, **_k: _Client()
    try:
        seen["result"] = fn(*a, **kw)
    finally:
        outreach.get_client = real
    return seen


def test_the_cold_email_prompt_carries_the_cleaned_link():
    out = _capture(outreach.draft_email, PROFILE, JOB, CONTACT)
    assert "https://acme.com/job/1" in out["user"]
    assert "utm_source" not in out["user"], "the tracking tag reached the prompt"


def test_the_cold_email_body_carries_it_even_when_the_model_drops_it():
    """The stub returns a body with no link at all, which is exactly the case the guarantee is
    for and the case a prompt-only implementation ships silently."""
    out = _capture(outreach.draft_email, PROFILE, JOB, CONTACT)
    assert "https://acme.com/job/1" in out["result"]["body"]


def test_the_followup_carries_it_too():
    """`send_followup` transmits `draft_body` BARE — no quoted original is appended — so a
    follow-up that omits the link names no role at all on a phone. Measured, not assumed; it is
    the reason this differs from the deck, which is offered once."""
    out = _capture(outreach.draft_followup, PROFILE, JOB,
                   dict(CONTACT, outreach_subject="Question about the role",
                        submitted_at="2026-08-01T00:00:00+00:00"), touch=1)
    assert "https://acme.com/job/1" in out["user"]
    assert "https://acme.com/job/1" in out["result"]["body"]


def test_a_targets_space_gets_no_posting_link():
    """There is no posting to link. A pitch is not an application (§Lessons 40 — the targets
    shape gets its own prompt rather than the job one with caveats).

    This exercises the ANCHOR guard: a real targets row is keyed `target:<space>:<slug>`, and
    `posting_link` refuses that outright. The SHAPE guard is a second, independent defence and
    needs its own test — see below, which is where this one was found to be passing for the
    wrong reason.
    """
    space = sp.from_template("partnerships", "Partnerships", "outreach")
    job = {"url": "target:partnerships:ridgeline", "company": "Ridgeline",
           "full_description": "Logistics.", "space_id": "partnerships"}
    out = _capture(outreach.draft_email, PROFILE, job, CONTACT, space=space)
    assert "THE POSTING" not in out["user"]


def test_the_shape_guard_holds_on_its_own():
    """Isolate the second guard, because the first one masks it completely.

    Found by mutation: deleting `if shape == "pipeline/targets"` from `job_link_for` changed
    nothing the suite could see, since every targets fixture also carried a `target:` anchor that
    `posting_link` rejects first. Two guards agreeing is not two guards tested — §Lessons 1's
    shape, an assertion that cannot fail when the thing under test is emptied.

    The state below is deliberately artificial: a targets Space holding a row with a real http
    URL should not occur. It is what a Space leak PRODUCES, and this codebase has shipped that
    exact bug twice (§Lessons 70 — `/api/import` and `_add_targets`), which is the argument for
    the guard existing rather than being simplified away as unreachable.
    """
    job = {"url": "https://acme.com/job/1", "company": "Ridgeline"}
    assert outreach.job_link_for(job, "pipeline/targets") == ""
    assert outreach.job_link_for(job, "pipeline/jobs") == "https://acme.com/job/1"


def test_a_text_never_carries_the_posting_link():
    """A URL from an unrecognised number is the strongest spam signal there is, and carriers
    filter on it. `_intro_deck_url` is already deliberately not consulted on this path; the same
    applies here, and "every email" was never "every channel"."""
    out = _capture(outreach.draft_sms, PROFILE, JOB,
                   dict(CONTACT, phone="+15125550100"))
    assert "https://acme.com/job/1" not in out["user"]
    assert "http" not in (out["result"].get("message") or "")


def test_the_linkedin_connect_note_never_carries_it():
    """300 characters, and LinkedIn penalises links in invite notes. The note comes out of the
    same draft as the email, so this is the one that could regress by accident."""
    out = _capture(outreach.draft_email, PROFILE, JOB, CONTACT)
    assert "https://acme.com/job/1" not in out["result"]["linkedin_note"]


def test_the_variant_records_it():
    """Otherwise "did adding the link change the reply rate" is unanswerable, which is the
    ceiling `draft_variant` exists to lift."""
    out = _capture(outreach.draft_email, PROFILE, JOB, CONTACT)
    assert "joblink" in out["result"]["variant"].split("+")


def test_no_link_means_no_block_and_no_tag():
    """A row whose URL is unusable must produce a prompt with no empty heading in it — a model
    handed "THE POSTING:" followed by nothing will invent one."""
    job = dict(JOB, url="./apply?jobId=x", application_url="")
    out = _capture(outreach.draft_email, PROFILE, job, CONTACT)
    assert "THE POSTING" not in out["user"]
    assert "joblink" not in out["result"]["variant"].split("+")
