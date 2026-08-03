"""A follow-up must know what the earlier ones said.

Reported from live use: touch 2 and 3 kept re-offering the intro deck that touch 1 had already
sent, in slightly different words. Two independent causes, both fixed here.

  1. `draft_followup` was shown only `contact.outreach_message` — the FIRST email, truncated to
     700 chars. It never saw the follow-ups already sent, so touch 2 did not know what touch 1
     said and touch 3 knew neither. The prompt has always said "do NOT repeat it" while being
     handed a third of what there was not to repeat.

  2. The deck was re-pitched deliberately: the prompt called it "the concrete thing this
     follow-up offers", and `ensure_intro_deck()` force-appended the link when the model left
     it out. That guarantee exists because a prompt instruction is not reliable (§Lessons 9,
     12) — applied unconditionally it guaranteed the repetition instead.

`conversation_transcript` and `touches.sent_touches` both already existed, and `_draft_reply`
already used them. This path simply never did — the §Lessons 39 shape: a function able to take
the context, called without it.
"""

from __future__ import annotations

import pytest

from applypilot.networking import outreach

PROFILE = {"personal": {"name": "Alejandro Diez"}}
JOB = {"url": "http://j/1", "title": "Applied AI Engineer", "company": "Saronic"}
DECK = "https://www.jorgealejandrodiez.com/intro/gina"


@pytest.fixture()
def prompt(monkeypatch):
    seen = {}

    class FakeClient:
        def chat(self, messages, **kw):
            seen["system"] = messages[0]["content"]
            seen["user"] = messages[1]["content"]
            return '{"subject": "Re: hi", "body": "A short nudge."}'

    monkeypatch.setattr(outreach, "get_client", lambda *_a, **_k: FakeClient())
    monkeypatch.setenv("INTRO_DECK_URL", "https://www.jorgealejandrodiez.com/intro/")
    monkeypatch.delenv("INTRO_DECK_PATHS", raising=False)   # base URL, no per-person slug
    return seen


def _contact(**over):
    c = {"id": "c1", "full_name": "Gina Johnson", "title": "Recruiter", "email": "g@x.com",
         "company": "Saronic", "outreach_subject": "quick q about the AI role",
         "outreach_message": "Hi Gina — I applied for the Applied AI Engineer role. "
                             "Here's a good intro deck we could go over during the call: "
                             "https://www.jorgealejandrodiez.com/intro/",
         "submitted_at": "2026-07-20T10:00:00+00:00", "replied_at": ""}
    c.update(over)
    return c


def _touch(seq, body, at="2026-07-24T10:00:00+00:00"):
    return {"seq": seq, "body": body, "subject": "Re: quick q", "sent_at": at}


# ── the drafter must SEE the earlier messages ───────────────────────────────

def test_previous_followups_reach_the_prompt(prompt):
    """The core bug. Touch 3 was drafted knowing only the first email."""
    prior = [_touch(1, "Wanted to bump this — did you get a chance to look at the deck?"),
             _touch(2, "Is someone else the right person for this?")]
    outreach.draft_followup(PROFILE, JOB, _contact(), touch=3, touches=prior)
    user = prompt["user"]
    assert "did you get a chance to look at the deck" in user, \
        "touch 1's text never reached the prompt, so touch 3 can repeat it"
    assert "Is someone else the right person" in user, "touch 2's text never reached the prompt"
    assert "do NOT repeat any of it" in user


def test_it_still_works_with_no_prior_touches(prompt):
    """The FIRST follow-up has nothing before it but the original email."""
    outreach.draft_followup(PROFILE, JOB, _contact(), touch=1, touches=[])
    assert "I applied for the Applied AI Engineer role" in prompt["user"]


def test_a_contact_with_no_history_at_all_does_not_crash(prompt):
    out = outreach.draft_followup(PROFILE, JOB, _contact(outreach_message=""), touch=1)
    assert out["body"]


# ── the deck is offered ONCE ────────────────────────────────────────────────

def test_the_deck_is_not_re_pitched_when_it_is_already_in_the_thread(prompt):
    """Four messages, four times "here's a deck" is the single most automated-sounding thing
    this sequence did."""
    outreach.draft_followup(PROFILE, JOB, _contact(), touch=2, touches=[])
    user = prompt["user"]
    assert "already sent" in user and "Do NOT paste it again" in user
    assert "INTRO DECK LINK (include it" not in user, \
        "the prompt still instructs the model to include a link they already have"


def test_the_deck_IS_offered_when_they_have_never_had_it(prompt):
    """The opposite failure — suppressing it always would silently drop the one concrete thing
    the follow-up has to offer."""
    never = _contact(outreach_message="Hi Gina — I applied for the role. Would love to chat.")
    outreach.draft_followup(PROFILE, JOB, never, touch=1, touches=[])
    assert "they have NOT been sent it" in prompt["user"]


def test_the_link_is_not_force_appended_onto_a_later_touch(prompt, monkeypatch):
    """`ensure_intro_deck` is what made this unavoidable: it re-added the URL even when the
    model had correctly left it out. It must only fire when they have never been sent it."""
    class Bare:
        def chat(self, messages, **kw):
            return '{"subject": "Re: hi", "body": "Quick nudge, no link here."}'
    monkeypatch.setattr(outreach, "get_client", lambda *_a, **_k: Bare())

    already = outreach.draft_followup(PROFILE, JOB, _contact(), touch=2, touches=[])
    assert "/intro/" not in already["body"], (
        "the deck link was force-appended to a follow-up for someone who already has it")

    fresh = outreach.draft_followup(
        PROFILE, JOB, _contact(outreach_message="Hi Gina — I applied."), touch=1, touches=[])
    assert "/intro/" in fresh["body"], "the deck was dropped for someone who never received it"


def test_a_personalised_link_still_matches_a_base_link_already_sent(prompt, monkeypatch):
    """Caught on LIVE data, not by this file's first draft.

    The earlier emails went out as ".../intro/"; INTRO_DECK_PATHS now builds ".../intro/michael".
    Matching the full personalised link finds nothing in the thread, so the deck gets re-pitched
    to someone who has already had it twice. The question is "have they been given the deck",
    and every variant shares the base.
    """
    monkeypatch.setenv("INTRO_DECK_PATHS", "1")
    got_base = _contact(outreach_message="here: https://www.jorgealejandrodiez.com/intro/")
    outreach.draft_followup(PROFILE, JOB, got_base, touch=2, touches=[])
    assert "already sent" in prompt["user"], (
        "a personalised link did not recognise the base link already in the thread")


def test_a_trailing_slash_does_not_reinstate_the_repetition(prompt):
    """The stored copy and the freshly-built URL can differ by exactly one character. A false
    "they have not seen it" puts the link back in every touch — the bug, restored quietly."""
    slashed = _contact(outreach_message="deck: https://www.jorgealejandrodiez.com/intro")
    outreach.draft_followup(PROFILE, JOB, slashed, touch=2, touches=[])
    assert "already sent" in prompt["user"], "a trailing slash defeated the already-sent check"


# ── the attachment is gone ──────────────────────────────────────────────────

def test_the_intro_deck_pdf_is_never_attached(tmp_path, monkeypatch):
    """It was 3.1 MB riding alongside a link to the same deck, so recipients got it twice.

    It was also on by ACCIDENT: `_intro_deck_path` defaulted OUTREACH_ATTACH_DECK to "1" while
    settings.py declared the default False, so `doctor --config` reported it off while every
    sent email carried it. A default living in two places is two defaults (§Lessons 21).
    """
    from applypilot.networking import gmail_send
    assert not hasattr(gmail_send, "_intro_deck_path"), \
        "the deck-attachment resolver is back"
    src = (gmail_send.__file__)
    text = open(src, encoding="utf-8").read()
    assert "Intro_Deck.pdf" not in text, "the deck is still being attached"
    # …and the résumé + cover letter, which ARE per-job and wanted, must survive.
    assert "_Resume{co}.pdf" in text and "_Cover_Letter{co}.pdf" in text


def test_the_removed_settings_are_undeclared_not_silently_ignored():
    """Left out of the registry deliberately. `doctor` reports an unknown variable in .env,
    which is how you find out you were relying on it."""
    from applypilot import settings
    assert "OUTREACH_ATTACH_DECK" not in settings._BY_NAME
    assert "INTRO_DECK_PATH" not in settings._BY_NAME
    assert "INTRO_DECK_URL" in settings._BY_NAME, "the LINK is the whole feature; it must stay"
