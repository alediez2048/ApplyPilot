"""When someone opens the intro deck, the next follow-up is about the deck.

The only point in this whole sequence where the recipient has DONE something. Every other
follow-up chases silence; this one can respond to an act. Two live contacts had a recorded open
when this was written, and one of them arrived three days after the email with no reply since.

Two decisions carry the feature, and both are borrowed from mistakes already paid for here:

**It REPLACES the ladder's intent, it does not join it.** The touch ladder arrives under
`THIS FOLLOW-UP:` and describes chasing someone who has done nothing. Appending "and ask about
the deck" leaves two instructions disagreeing and the heading wins — which is exactly how SMS
drafts kept asking whether the first email had arrived, of people who had already answered
(§Lessons 40).

**It must never reveal that we know.** The signal comes from a beacon on the sender's own site.
Telling a stranger their reading was watched converts the best signal in the sequence into the
reason they stop replying, and there is no second impression. The codebase already bans this
shape for `noticed` — "NEVER ANNOUNCE THE NOTICING" — and the stakes are higher here, because a
LinkedIn post is public and someone's browsing is not.
"""

from __future__ import annotations

import pytest

from applypilot.domain.interactions import deck_opened_since_we_wrote as opened
from applypilot.networking import outreach

JOB = {"url": "https://boards.example/acme/job/1", "title": "Applied AI Engineer",
       "company": "Acme", "site": "Greenhouse", "full_description": "Build agent pipelines.",
       "space_id": "job-search"}
PROFILE = {"personal": {"full_name": "Jorge Alejandro Diez", "preferred_name": "Alejandro",
                        "intro_deck_url": "https://example.test/intro/"}}


def _contact(**kw):
    base = {"id": "c1", "full_name": "Dana Whitfield", "title": "Engineering Manager",
            "company": "Acme", "email": "j@acme.test", "outreach_subject": "About the role",
            "submitted_at": "2026-08-06T20:00:00+00:00", "replied_at": "",
            "deck_viewed_at": "", "deck_last_at": ""}
    base.update(kw)
    return base


# ── when it counts as engagement ────────────────────────────────────────────

def test_an_open_after_our_email_counts():
    """The genuine live shape: emailed, opened three days later, no reply since."""
    assert opened(_contact(deck_last_at="2026-08-09T19:02:51.852Z")) is True


def test_an_open_BEFORE_our_email_does_not():
    """The other live shape, and the reason this is anchored on our last send rather than on
    "has ever opened".

    The open is stamped 19:28:19 and our email went at 19:29:47 — ninety seconds LATER. That is
    the operator previewing their own /intro/<name> link before hitting send, which §Lessons 64
    records as the recipient having read it. A "has ever opened" rule calls that engagement and
    writes a follow-up asking what somebody thought of a deck they had not yet been sent.
    """
    c = _contact(deck_last_at="2026-08-06T19:28:19.786Z",
                 submitted_at="2026-08-06T19:29:47.027792+00:00")
    assert opened(c) is False


def test_an_open_before_the_last_FOLLOW_UP_does_not_count_again():
    """Otherwise every remaining touch keeps asking about the same click, which is nagging about
    something they already declined to answer once."""
    c = _contact(deck_last_at="2026-08-09T19:02:51Z")
    touches = [{"sent_at": "2026-08-10T09:00:00+00:00"}]
    assert opened(c, touches) is False


def test_an_open_after_the_last_follow_up_counts():
    c = _contact(deck_last_at="2026-08-11T09:00:00+00:00")
    touches = [{"sent_at": "2026-08-10T09:00:00+00:00"}]
    assert opened(c, touches) is True


def test_an_open_at_the_exact_instant_we_sent_does_not_count():
    """A tie-break, made explicit because mutation showed `>` and `>=` were interchangeable.

    Unreachable in practice — the beacon's clock and ours are different machines and both carry
    sub-second precision — but the direction is a real choice: if the two cannot be ordered, we
    cannot say the click came after the email, so it is not engagement. Same conservative
    direction as the preview case above, and for the same reason: the cost of a false positive
    is a message asking what somebody thought of a deck they may not have been sent.
    """
    c = _contact(deck_last_at="2026-08-06T20:00:00+00:00",
                 submitted_at="2026-08-06T20:00:00+00:00")
    assert opened(c) is False


def test_a_reply_outranks_it():
    """Once someone writes back the sequence is terminal and the conversation decides what to
    say next. A deck click by someone who already replied is not a follow-up trigger."""
    c = _contact(deck_last_at="2026-08-09T19:02:51Z", replied_at="2026-08-08T10:00:00+00:00")
    assert opened(c) is False


def test_no_open_is_not_engagement():
    assert opened(_contact()) is False


def test_a_naive_timestamp_does_not_explode():
    """Older rows have no timezone, and subtracting a naive from an aware datetime raises and
    500s the caller (§Lessons 6). `parse_ts` is the one guard and this pins that it is used."""
    c = _contact(deck_last_at="2026-08-09T19:02:51", submitted_at="2026-08-06T20:00:00")
    assert opened(c) is True


def test_deck_last_at_wins_over_deck_viewed_at():
    """`deck_viewed_at` is the FIRST open, `deck_last_at` the most recent. Someone who opened it
    a fortnight ago and again this morning has just done something."""
    c = _contact(deck_viewed_at="2026-07-01T09:00:00Z", deck_last_at="2026-08-09T19:02:51Z")
    assert opened(c) is True


# ── what the prompt then says ───────────────────────────────────────────────

def _prompt(contact, touch=1, touches=None) -> str:
    seen = {}

    class _Client:
        def chat(self, messages, **_):
            seen["user"] = messages[1]["content"]
            return '{"subject":"Re: About the role","body":"Hi.\\n\\nThanks,\\nAlejandro"}'

    real = outreach.get_client
    outreach.get_client = lambda *_a, **_k: _Client()
    try:
        outreach.draft_followup(PROFILE, JOB, contact, touch=touch, touches=touches)
    finally:
        outreach.get_client = real
    return seen["user"]


def test_the_ladder_intent_is_replaced_not_appended():
    """The heart of it. Both present means two instructions disagreeing under one heading, and
    the ladder wins — the SMS bug, rebuilt (§Lessons 40)."""
    user = _prompt(_contact(deck_last_at="2026-08-09T19:02:51Z"))
    assert "looked at the intro deck" in user
    for ladder in outreach._TOUCH_INTENT.values():
        assert ladder not in user, "the cold-chasing ladder is still in the prompt beside it"


def test_an_unopened_deck_leaves_the_ladder_alone():
    """Guard the guard: replacing it unconditionally would pass the test above while breaking
    every ordinary follow-up."""
    user = _prompt(_contact())
    assert outreach._TOUCH_INTENT[1] in user
    assert "looked at the intro deck" not in user


def test_the_prompt_forbids_revealing_that_we_know():
    """The line between a warm follow-up and telling a stranger they were watched."""
    user = _prompt(_contact(deck_last_at="2026-08-09T19:02:51Z"))
    assert "NEVER SAY, HINT OR IMPLY THAT YOU KNOW THEY OPENED IT" in user
    # And the test of the rule rather than its wording: a sentence that only makes sense to
    # someone who DID open it is the thing being banned.
    assert "would not make sense to someone who had NOT opened it" in user


@pytest.mark.parametrize("phrase", ["I saw you had a look", "since you checked out the deck",
                                    "I noticed you opened"])
def test_the_banned_shapes_are_named(phrase):
    """Naming the phrasings is what worked for SMS. §Lessons 42: the prompt said to concede the
    channel and the model produced the SAME sentence in 5 of 5 drafts, so a rule stated only in
    the abstract is not a rule."""
    assert phrase in _prompt(_contact(deck_last_at="2026-08-09T19:02:51Z"))


def test_the_deck_link_is_not_re_pitched_to_someone_who_read_it():
    """`deck_sent` is computed by string-matching the transcript, and an open is proof of receipt
    that no string match can override. Without this the prompt offers the deck while the intent
    asks what they made of it — the same contradiction one level down."""
    user = _prompt(_contact(deck_last_at="2026-08-09T19:02:51Z"))
    assert "they have NOT been sent it" not in user
    assert "already sent" in user


def test_it_applies_on_the_final_touch_too():
    """Touch 3 says "this is the LAST message". Someone who just read the deck is the worst
    possible person to send that to."""
    user = _prompt(_contact(deck_last_at="2026-08-09T19:02:51Z"), touch=3)
    assert "looked at the intro deck" in user
    assert outreach._TOUCH_INTENT[3] not in user
