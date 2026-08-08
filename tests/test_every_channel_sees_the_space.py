"""CTX-3 — the operator's layers reach every channel, not just the cold email.

Measured on 2026-08-07, before this ticket:

    draft_email              tone ✓   premise ✓   context ✓
    draft_followup           tone ✗   premise ✗   context ✗   (takes `space`, reads only `shape`)
    draft_for_channel        tone ✗   premise ✗   context ✗
    draft_linkedin_followup  tone ✗   premise ✗   context ✗   (takes `space`, reads only `shape`)
    draft_reply              — cannot accept a manifest at all —
    draft_sms                — cannot accept a manifest at all —

So the campaign's standing voice applied to a cold email and stopped: the follow-up, the text
and the reply were written as though no Space existed. That is worse than it sounds, because a
message with no campaign voice is a *perfectly good message* — nothing errors, nothing renders
wrong, and the only way to see it is to ask which parameters exist.

Same shape as SPACE-4's own bug one layer down: `followup_panel` spelled out
`buckets[EMAIL.name], buckets[LINKEDIN.name]` by hand, so a third channel passed correctly
through the whole engine and vanished at the return statement.
"""

from __future__ import annotations

import inspect

import pytest

from applypilot.domain import space as sp
from applypilot.networking import outreach

#: A voice that appears NOWHERE in this codebase. `test_adding_a_channel_needs_no_schema_change`
#: originally named SMS and silently broke the day SMS shipped — a fixture that collides with
#: something real starts passing for the wrong reason. Checked by a test below.
VOICE = "Write like a lighthouse keeper filing a weather report."
PREMISE = "Every role here is downstream of ten weeks building agents full time."
KNOWN = "Their intake team is rebuilding on agents after the Q2 reorg; I met Dana at PyCon."

PROFILE = {"personal": {"full_name": "Alejandro Diez", "intro_deck_url": "",
                        "scheduling_link": "https://cal.test/me"}}
JOB = {"url": "http://j/1", "title": "Applied AI Engineer", "company": "Acme",
       "site": "Greenhouse", "full_description": "You will build agent pipelines end to end.",
       "space_id": "job-search", "applied_at": "2026-08-01T10:00:00+00:00",
       "job_context": KNOWN}
CONTACT = {"id": "c1", "full_name": "Sarah Chen", "title": "Head of Engineering",
           "company": "Acme", "email": "s@acme.test", "match_reason": "works at the company",
           "outreach_message": "The first email.", "outreach_subject": "Hello",
           "submitted_at": "2026-08-02T10:00:00+00:00", "sent_message_id": "m1",
           "linkedin_url": "https://linkedin.com/in/sarah", "dm_status": "sent",
           "dm_sent_at": "2026-08-02T10:00:00+00:00", "phone": "+15125551234"}
THREAD = [{"direction": "in", "snippet": "Thanks for reaching out, what does the team look like?",
           "from_name": "Sarah Chen", "at": "2026-08-03T10:00:00+00:00"}]


def _space(**kw):
    return sp.from_template("job-search", "Job Search", "jobs",
                            tone=VOICE, offer=PREMISE, **kw)


def _capture(fn, **kw):
    """Run one draft entry point against a stub client and return the user prompt."""
    captured = {}

    class _Client:
        def chat(self, messages, **_):
            captured["user"] = messages[1]["content"]
            return ('{"subject":"s","body":"b","linkedin_note":"n","message":"m",'
                    '"note":"n"}')

    real = outreach.get_client
    outreach.get_client = lambda *_a, **_k: _Client()
    try:
        fn(**kw)
    finally:
        outreach.get_client = real
    return captured["user"]


#: Every entry point, with the arguments it needs to reach its own prompt. `draft_for_channel`
#: is included as itself AND is what routes to sms/linkedin in production, so both the router
#: and the leaf are covered.
ENTRY_POINTS = {
    "draft_email": lambda space: _capture(
        outreach.draft_email, profile=PROFILE, job=JOB, contact=CONTACT, space=space),
    "draft_followup": lambda space: _capture(
        outreach.draft_followup, profile=PROFILE, job=JOB, contact=CONTACT, touch=1, space=space),
    "draft_linkedin_followup": lambda space: _capture(
        outreach.draft_linkedin_followup, profile=PROFILE, job=JOB, contact=CONTACT, touch=1,
        space=space),
    "draft_reply": lambda space: _capture(
        outreach.draft_reply, profile=PROFILE, job=JOB, contact=CONTACT, thread=THREAD,
        their_reply="what does the team look like?", space=space),
    "draft_sms": lambda space: _capture(
        outreach.draft_sms, profile=PROFILE, job=JOB, contact=CONTACT, touch=0, space=space),
    "draft_for_channel/sms": lambda space: _capture(
        outreach.draft_for_channel, channel="sms", profile=PROFILE, job=JOB, contact=CONTACT,
        touch=1, space=space),
    "draft_for_channel/email": lambda space: _capture(
        outreach.draft_for_channel, channel="email", profile=PROFILE, job=JOB, contact=CONTACT,
        touch=1, space=space),
}


# ── the campaign reaches every channel ──────────────────────────────────────

@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_the_campaign_voice_reaches_every_channel(name):
    """Before CTX-3 this passed for exactly one of the seven."""
    assert VOICE.strip(), "an empty fixture makes this assertion vacuous"
    assert VOICE in ENTRY_POINTS[name](_space())


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_the_premise_reaches_every_channel(name):
    assert PREMISE in ENTRY_POINTS[name](_space())


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_the_row_context_reaches_every_channel(name):
    assert KNOWN in ENTRY_POINTS[name](_space())


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_no_space_adds_nothing_anywhere(name):
    """Additive on every path, not only the one with a golden file."""
    got = ENTRY_POINTS[name](None)
    assert VOICE not in got
    assert PREMISE not in got
    assert "THE PREMISE OF THIS CAMPAIGN" not in got


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_the_voice_is_the_last_thing_before_the_instruction(name):
    """The convention `draft_email` already established: a constraint placed above the
    scheduling and deck blocks competes with them and loses (§Lessons 40)."""
    got = ENTRY_POINTS[name](_space())
    after = got[got.index(VOICE):]
    assert "Write the" in after or "Return the JSON" in after
    # Nothing else the operator supplied may come after it.
    assert PREMISE not in after and KNOWN not in after


# ── the guard that would have caught this ───────────────────────────────────

DRAFT_FUNCTIONS = ["draft_email", "draft_followup", "draft_for_channel", "draft_reply",
                   "draft_linkedin_followup", "draft_sms"]


@pytest.mark.parametrize("fn", DRAFT_FUNCTIONS)
def test_every_draft_entry_point_accepts_a_space(fn):
    """Mechanical, cheap, and it is what would have found this weeks ago. A new entry point
    added later fails by default, which is the point — `draft_reply` and `draft_sms` were
    written without the parameter and nothing noticed, because a message with no campaign voice
    is a perfectly good message."""
    sig = inspect.signature(getattr(outreach, fn))
    assert "space" in sig.parameters, f"{fn} cannot see the Space it is drafting for"


def test_the_test_voice_is_genuinely_unknown_to_the_codebase():
    """§Lessons 60's cousin. A fixture colliding with real copy starts passing for the wrong
    reason the moment that copy ships — which is exactly how the channel version of this test
    broke when SMS became real."""
    import pathlib
    root = pathlib.Path(__file__).parent.parent / "src"
    for f in root.rglob("*.py"):
        assert VOICE not in f.read_text(encoding="utf-8"), f"{f} already contains the fixture"


# ── what CTX-3 must not break ───────────────────────────────────────────────

def test_a_text_never_consults_the_deck():
    """Threading `space` into `draft_sms` hands it `offer_deck`, and a URL from an unrecognised
    number is the strongest spam signal there is. Asserted, not merely avoided."""
    profile = dict(PROFILE)
    profile["personal"] = dict(PROFILE["personal"], intro_deck_url="https://deck.test/intro/")
    got = _capture(outreach.draft_sms, profile=profile, job=JOB, contact=CONTACT, touch=0,
                   space=_space(offer_deck=True))
    assert "deck.test" not in got
    assert "http" not in got.replace("https://cal.test/me", "")


def test_the_short_channels_get_the_short_guidance():
    """A paragraph of "how to use it" in a 300-character message crowds out the message. The
    field is never DROPPED though — a channel silently missing a layer is the bug this ticket
    exists to close."""
    long_form = ENTRY_POINTS["draft_email"](_space())
    brief = ENTRY_POINTS["draft_sms"](_space())
    assert PREMISE in long_form and PREMISE in brief
    assert len(long_form[long_form.index(PREMISE):]) > len(brief[brief.index(PREMISE):])


def test_the_reply_still_answers_what_they_said():
    """The layers are additive to a live conversation, never a replacement for it."""
    got = ENTRY_POINTS["draft_reply"](_space())
    assert "what does the team look like?" in got
    assert got.index("what does the team look like?") < got.index(VOICE)
