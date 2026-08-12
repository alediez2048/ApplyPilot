"""What a ROW is and what the EMAIL is are two decisions — SHEET-1 C3.

They used to be one. `draft_email` branched on `shape`, so `pipeline/targets` forced
`_PITCH_SYSTEM` and there was no way to express "a card per company, written in the job search's
own words". That is exactly what a spreadsheet of companies needs, and it was unreachable.

`Space.voice` splits them. It defaults to "" and the resolver returns precisely what the shape
branch used to hardcode, so no existing Space moves — `tests/golden/jobs_outreach_prompt.txt`
staying byte-identical through this change is the proof, not a claim.

The third voice needed its own prompt rather than a flag on an existing one, and
`_premise_block`'s own text is why: it says *"It is background, not the subject. A message that
is only the premise is about the sender"* and *"say it in your own words each time, or leave it
out"*. Both are correct where they are — in a jobs Space the POSTING is the subject. Appending
"…but here it is the subject" to that is §Lessons 40, two instructions disagreeing, and the
heading wins every time.
"""

from __future__ import annotations

import pytest

from applypilot.domain import space as sp
from applypilot.networking import outreach


def flat(s: str) -> str:
    """Collapse the prompt's own line wrapping before matching a sentence in it.

    These are hand-wrapped paragraphs, so "…the sender named\\n  one" does not contain "named
    one" — an assertion that passes only while a line happens to break in the right place tests
    the formatting, not the rule.
    """
    return " ".join((s or "").split())


# ── the split ───────────────────────────────────────────────────────────────

def test_the_default_voice_is_exactly_what_the_shape_branch_used_to_do():
    """Nothing moves for an existing Space. If this ever changes, every live campaign silently
    starts writing a different kind of email."""
    jobs = sp.from_template("j", "J", "jobs")
    outreach_sp = sp.from_template("p", "P", "outreach")
    assert jobs.voice == "" and outreach_sp.voice == ""
    assert jobs.voice_or_default() == "jobseeker"
    assert outreach_sp.voice_or_default() == "pitch"


def test_a_space_can_choose_a_voice_its_shape_would_not_pick():
    """The whole point: a company-shaped row written in the job seeker's campaign paragraph."""
    s = sp.from_template("s", "S", "outreach", voice="premise")
    assert s.shape == sp.TARGETS_SHAPE
    assert s.voice_or_default() == "premise"


def test_an_unknown_voice_is_refused_at_construction():
    """It would fall through to the shape default and send the WRONG KIND OF EMAIL silently —
    a typo'd `voice="premis"` writing pitches from a job search, with nothing saying so."""
    with pytest.raises(ValueError) as e:
        sp.from_template("s", "S", "outreach", voice="premis")
    assert "voice" in str(e.value)


def test_the_sheet_template_is_a_company_row_with_the_job_seekers_words():
    s = sp.from_template("sheets", "Sheets", "sheet")
    assert s.shape == sp.TARGETS_SHAPE       # rows are companies
    assert s.voice_or_default() == "premise"  # written from the campaign paragraph
    assert s.terminal == "interview"          # it is still a job search
    assert s.tailor_docs is False             # no posting, nothing to tailor per row


# ── the two premise blocks say opposite things, on purpose ──────────────────

SPACE = sp.from_template("s", "S", "sheet", offer="Ten years in enterprise tech, now building AI.")


def test_the_premise_is_the_subject_here_and_background_everywhere_else():
    """A shared block means §Lessons 40 was re-introduced: the model is told the paragraph is
    optional and then expected to write the email about it."""
    led = flat(outreach._premise_led_block(SPACE))
    bg = flat(outreach._premise_block(SPACE))
    assert led != bg

    assert "SUBSTANCE" in led and "first two sentences" in led
    assert "leave it out" not in led, "the premise-led block still permits omitting the premise"

    assert "background, not the subject" in bg
    assert "leave it out" in bg, "the original block was changed; jobs Spaces depend on it"


def test_both_blocks_keep_facts_never_phrasing():
    """The one rule that survives the inversion, and it matters MORE here: `offer` is per SPACE,
    so a parroted sentence reaches every inbox in the campaign — a larger exposure than
    `job_context` (per row) or `noticed` (per person)."""
    for block in (outreach._premise_led_block(SPACE), outreach._premise_block(SPACE)):
        b = flat(block)
        assert "FACTS" in b
        assert "never sentences to reuse" in b or "never sentences to reuse" in b.lower()
        assert "wording" in b, "the block no longer warns against reusing the premise verbatim"


def test_an_empty_premise_produces_no_block():
    assert outreach._premise_led_block(sp.from_template("s", "S", "sheet")) == ""


# ── the third system prompt ─────────────────────────────────────────────────

def test_the_premise_prompt_denies_both_of_the_other_two_framings():
    """`_SYSTEM` opens "a job seeker reaching out to someone at a company they just applied to";
    `_PITCH_SYSTEM` opens "proposing a piece of work". Neither is true here, and no amount of
    appended text makes an opening line mean something else."""
    s = flat(outreach._PREMISE_SYSTEM)
    assert "Not a job application" in s
    assert "no posting" in s
    assert "Not a sales pitch" in s


def test_it_forbids_implying_an_application_or_naming_a_role():
    """There is no posting on these cards. §Lessons 84: an invented role is worse than none."""
    s = flat(outreach._PREMISE_SYSTEM)
    assert "never reference a specific role unless the sender named one" in s
    assert "Never claim you applied to anything" in s


def test_it_keeps_the_rules_that_are_about_writing_to_a_stranger():
    """Most of `_PITCH_SYSTEM` is shared verbatim, deliberately — those rules are about someone
    who did not ask to hear from you, which is equally true here."""
    s = flat(outreach._PREMISE_SYSTEM)
    assert "Under 120 words" in s
    assert "Exactly ONE question" in s
    assert "explicit out" in s
    assert "did not ask to hear from you" in s
    # §Lessons 42: several people at one company get these and they sit near each other.
    assert "SEVERAL PEOPLE AT THE SAME COMPANY" in s
    # The em-dash rule is belt-and-braces with `strip_ai_dashes`, on every prompt.
    assert "NEVER use an em dash" in s


def test_it_refuses_to_invent_anything_about_the_company():
    """Usually the ONLY facts are a name, a title and an employer. An invented product or
    funding round is the one error that cannot be recovered from."""
    assert "Never invent anything about their company" in flat(outreach._PREMISE_SYSTEM)


# ── the user prompt ─────────────────────────────────────────────────────────

CONTACT = {"full_name": "Dana Okafor", "title": "VP Engineering"}


def _prompt(space=SPACE, about="", noticed=""):
    return outreach._premise_user_prompt(
        ["Alejandro, Austin TX"], CONTACT, "Ridgeline", about, noticed,
        "", "", "", "", [], space=space)


def test_the_prompt_carries_the_person_and_the_premise():
    p = _prompt()
    assert "Dana Okafor" in p and "VP Engineering" in p and "Ridgeline" in p
    assert "Ten years in enterprise tech" in p


def test_an_unknown_company_is_stated_not_left_blank():
    """A model handed "WHAT THIS COMPANY DOES:" followed by nothing invents something, and an
    invented fact about the recipient's own employer is unrecoverable."""
    p = flat(_prompt())
    assert "not recorded" in p
    assert "Do not invent a product" in p


def test_what_the_operator_typed_about_the_company_is_used_when_present():
    p = _prompt(about="Freight brokerage, 300 people, Austin HQ.")
    assert "Freight brokerage" in p
    assert "not recorded" not in p


def test_noticing_is_never_announced():
    """§Lessons 83: a sentence whose job is to report that you looked is the most recognisable
    automated-outreach shape there is."""
    p = flat(_prompt(noticed="Wrote a post about freight routing"))
    assert "NEVER ANNOUNCE THE NOTICING" in p


def test_must_mention_reaches_the_premise_prompt_too():
    """§Lessons 49: a rule wired into one of several prompts is not wired. Gauntlet's premise
    named GauntletAI twice and zero of eight drafts mentioned it — the enforcement is the retry,
    and it has to be asked for here as well."""
    s = sp.from_template("s", "S", "sheet", offer="x", must_mention=("GauntletAI",))
    assert "GauntletAI" in _prompt(space=s)


def test_the_premise_prompt_names_no_posting_and_no_role():
    """The jobs prompt carries `posting_ref` and a role. Neither exists on these cards, and a
    heading for a fact that is not there is how one gets invented."""
    p = _prompt()
    for absent in ("REQUISITION", "the role", "you applied"):
        assert absent not in p


# ── selection ───────────────────────────────────────────────────────────────

def test_draft_email_picks_the_prompt_from_the_voice_not_the_shape():
    """Read off the source rather than by calling the model. The branch is what changed; the
    behavioural check is the golden file staying byte-identical for the jobs path."""
    import inspect
    src = inspect.getsource(outreach.draft_email)
    assert 'voice == "premise"' in src
    assert "_PREMISE_SYSTEM" in src
    assert 'if shape == "pipeline/targets":' not in src, (
        "the prompt is still chosen by shape, so a targets Space cannot pick another voice")


def test_the_premise_box_is_labelled_by_the_VOICE_not_the_shape():
    """A company-shaped Space in the premise voice was labelled "Your offer" and described as
    *"what you are proposing"* — but the field feeds a job seeker's paragraph about themselves.

    §Lessons 72's shape: `offer` was once declared, documented for the jobs case, and wired into
    one of two shapes while sitting in a box hidden on the other — so it could be read by nothing
    AND typed by no one. A field the operator cannot name correctly is the same failure one step
    later.
    """
    assert sp.offer_copy(sp.TARGETS_SHAPE)["title"] == "Your offer"
    assert sp.offer_copy(sp.TARGETS_SHAPE, "pitch")["title"] == "Your offer"
    # Same shape, premise voice -> the jobs wording, because that is what the field now means.
    assert sp.offer_copy(sp.TARGETS_SHAPE, "premise") == sp.offer_copy(sp.JOBS_SHAPE)
    assert "premise" in sp.offer_copy(sp.TARGETS_SHAPE, "premise")["title"].lower()


def test_the_operators_per_row_context_reaches_this_prompt_too():
    """CTX-2's `job_context` was wired into the jobs prompt and nowhere else, so a Space whose
    rows are companies had an operator-typed context box feeding nothing — §Lessons 49 and 72,
    which is the exact shape of `offer` being declared, documented for one case, and read on one
    of two shapes."""
    job = {"job_context": "Their CTO spoke at AITX about routing costs."}
    p = flat(outreach._premise_user_prompt(
        ["Alejandro"], CONTACT, "Ridgeline", "", "", "", "", "", "", [],
        space=SPACE, known_block=outreach._known_block(job)))
    assert "AITX about routing costs" in p
    assert "WHAT THE SENDER KNOWS ABOUT THIS COMPANY" in p


def test_the_two_company_blocks_are_distinct():
    """What the company IS (the About column, public, could be automated later) and what the
    OPERATOR knows (typed, not public) are different facts and must not collapse into one
    heading — the model treats a heading as the thing's identity."""
    job = {"job_context": "Their CTO spoke at AITX."}
    p = flat(outreach._premise_user_prompt(
        ["Alejandro"], CONTACT, "Ridgeline", "Freight brokerage, 300 people.", "",
        "", "", "", "", [], space=SPACE, known_block=outreach._known_block(job)))
    assert "WHAT THIS COMPANY DOES" in p and "Freight brokerage" in p
    assert "WHAT THE SENDER KNOWS ABOUT THIS COMPANY" in p and "AITX" in p


def test_no_context_adds_no_heading():
    p = flat(outreach._premise_user_prompt(
        ["Alejandro"], CONTACT, "Ridgeline", "", "", "", "", "", "", [], space=SPACE))
    assert "WHAT THE SENDER KNOWS" not in p
