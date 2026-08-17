"""When they owe US a reply, the draft is a NUDGE — not another answer.

Reported as *"this follow up is repeating myself, something is still off"*, on the live Sentilink
card, and it was exact. Liz acknowledged the application; we answered *"Looking forward to hearing
from your team. Let me know if you need anything else from me in the meantime"*; three days later
the drafter produced *"look forward to hearing back from your team soon. If there's anything else
you need from my side, just let me know."* A paraphrase of our own message.

**Nothing was missing from the transcript.** Our reply was in it, directly under a line reading
"Everything above marked YOU is already in their inbox. Do not repeat any of it." That rule lost
to the task, which said *"Answer what they said"* — and answering a message we have already
answered can only produce the answer again (§Lessons 40: two instructions disagreeing is a code
bug, and the one naming the task wins).

`conversation_state` had known the whole time — `awaiting_them`, `stalled`, 91 hours — and no
drafter had ever been told (§Lessons 21).
"""

from __future__ import annotations

from applypilot.networking import outreach

ANSWER_TASK = "Answer what they said"


def _m(direction, snippet, at, name="Liz"):
    # `from_addr` is not decoration: `conversation_state` drops inbound mail from robots, and
    # `is_robot("")` is true — so a fixture without one has NO conversation at all and every
    # assertion here would measure the empty case (§Lessons 13).
    return {"message_id": f"m{at}{direction}", "thread_id": "t1", "direction": direction,
            "from_addr": "liz@sentilink.com" if direction == "in" else "me@x.test",
            "from_name": name if direction == "in" else "", "sent_at": at,
            "subject": "Re: the role", "snippet": snippet}


#: They wrote, we answered, they went quiet — the reported case.
THEY_OWE_US = [
    _m("in", "Thanks for applying! We will review and be in touch.", "2026-08-13T10:00:00+00:00"),
    _m("out", "Thanks for confirming. Looking forward to hearing from your team.",
       "2026-08-13T20:00:00+00:00"),
]

#: They wrote last. This one really is a reply.
WE_OWE_THEM = [
    _m("out", "Hi Liz, I just applied for the role.", "2026-08-13T10:00:00+00:00"),
    _m("in", "Are you free Thursday for a quick call?", "2026-08-14T10:00:00+00:00"),
]


# ── the task is REPLACED, not caveated ──────────────────────────────────────

def test_a_thread_they_owe_us_asks_for_a_NUDGE():
    task = outreach._turn_task(THEY_OWE_US)
    assert "NUDGE, not a reply" in task


def test_the_answer_task_is_GONE_not_softened():
    """Appending "…and do not repeat yourself" is the fix that does not work: that sentence was
    already in the prompt, one paragraph above, and the model ignored it."""
    assert ANSWER_TASK not in outreach._turn_task(THEY_OWE_US)


def test_a_thread_where_THEY_wrote_last_is_still_a_reply():
    """The narrowing has to be real. If this said NUDGE too, answering a live question would
    become impossible — which is the opposite failure and worse."""
    task = outreach._turn_task(WE_OWE_THEM)
    assert ANSWER_TASK in task and "NUDGE" not in task
    assert outreach._turn_block(WE_OWE_THEM) == ""


def test_a_thread_with_no_inbound_at_all_is_left_alone():
    """`conversation_state` returns None for our own outreach — a follow-up ladder, not a
    conversation — and this must not invent a state for it."""
    only_ours = [_m("out", "Hi Liz, I just applied.", "2026-08-13T10:00:00+00:00")]
    assert outreach._turn_block(only_ours) == ""
    assert ANSWER_TASK in outreach._turn_task(only_ours)


def test_the_nudge_task_forbids_the_exact_phrases_that_were_repeated():
    """Named, because these are the sentences that actually came back.

    "asking whether they need anything from you" was added after GENERATING against the live model
    rather than from reading the prompt: the first tightened version still produced *"Is there
    anything else you need from me"*, which is a paraphrase of the sent message's *"Let me know if
    you need anything else from me in the meantime"*. Banning the offer was not the same as
    banning the question (§Lessons 42 — generate before believing a prompt).
    """
    task = outreach._turn_task(THEY_OWE_US)
    assert "look forward to hearing" in task
    assert "thanking them again" in task
    assert "offering to send anything else" in task
    assert "ASKING WHETHER THEY NEED ANYTHING FROM YOU" in task
    assert "Ask for something instead" in task


def test_the_nudge_asks_for_something_rather_than_just_checking_in():
    """A nudge with no ask is the same dead end in a shorter form."""
    task = outreach._turn_task(THEY_OWE_US)
    assert "what you actually want NOW" in task
    assert "timeline" in task


# ── the state is asserted before the task ───────────────────────────────────

def test_the_block_states_that_our_turn_was_already_taken():
    block = outreach._turn_block(THEY_OWE_US)
    assert "yours was already taken" in block
    assert "nothing left to answer" in block
    assert "they owe YOU" in block


def test_the_block_QUOTES_our_last_message_as_the_thing_not_to_rewrite():
    """The general "do not repeat" line is what the model ignored, so the sentence it must not
    rewrite is shown beside the task rather than left to be inferred from the transcript."""
    block = outreach._turn_block(THEY_OWE_US)
    assert "Looking forward to hearing from your team" in block
    assert "do not paraphrase it" in block


def test_the_block_says_HOW_LONG_they_have_been_quiet():
    block = outreach._turn_block(THEY_OWE_US)
    assert "day" in block, block


def test_our_last_message_RESETS_on_every_inbound():
    """What matters is the message they have not answered. An outbound one from BEFORE their reply
    was already answered — by the reply.

    The trailing EMPTY outbound row is what makes this test able to fail, and it is the ordinary
    shape rather than a contrived one: the poller records our own sent messages with no text, so
    the newest outbound row is routinely blank. Without the reset, `ours` falls back to the
    message from before their reply — and the prompt then tells the model not to repeat a sentence
    they have already responded to, while leaving the one they have not seen unguarded.

    With the last outbound carrying text, both a resetting and a non-resetting implementation
    return the same row, so a thread like that cannot see the difference at all.
    """
    thread = [
        _m("out", "FIRST, long since answered", "2026-08-01T10:00:00+00:00"),
        _m("in", "Thanks, we will review.", "2026-08-02T10:00:00+00:00"),
        _m("out", "", "2026-08-03T10:00:00+00:00"),          # the poller's empty row
    ]
    assert outreach._our_last_in_thread(thread) is None, \
        "a message from before their reply was offered as the one they have not answered"
    block = outreach._turn_block(thread)
    assert "they owe YOU" in block, "the turn is still theirs"
    assert "FIRST" not in block


def test_the_message_they_have_not_answered_IS_quoted_when_we_have_it():
    thread = [
        _m("out", "FIRST, long since answered", "2026-08-01T10:00:00+00:00"),
        _m("in", "Thanks, we will review.", "2026-08-02T10:00:00+00:00"),
        _m("out", "SECOND, still unanswered", "2026-08-03T10:00:00+00:00"),
    ]
    ours = outreach._our_last_in_thread(thread)
    assert ours and "SECOND" in ours["snippet"]
    block = outreach._turn_block(thread)
    assert "SECOND" in block and "FIRST" not in block


def test_an_empty_outbound_row_is_not_quoted_as_our_last_message():
    """The poller writes our own sent messages with no text at all, so the newest outbound row is
    routinely blank — quoting it would put an empty pair of quotes in the prompt."""
    thread = [
        _m("in", "We will review.", "2026-08-02T10:00:00+00:00"),
        _m("out", "Looking forward to hearing from your team.", "2026-08-03T10:00:00+00:00"),
        _m("out", "", "2026-08-04T10:00:00+00:00"),
    ]
    ours = outreach._our_last_in_thread(thread)
    assert ours and "Looking forward" in ours["snippet"]


def test_the_block_survives_our_message_having_no_stored_text():
    """It must still assert whose turn it is — that is the half that changes the task."""
    thread = [
        _m("in", "We will review.", "2026-08-02T10:00:00+00:00"),
        _m("out", "", "2026-08-03T10:00:00+00:00"),
    ]
    block = outreach._turn_block(thread)
    assert "they owe YOU" in block
    assert '""' not in block, "an empty quote block was rendered"


# ── it reaches the prompt ───────────────────────────────────────────────────

def test_the_drafter_actually_USES_both(monkeypatch):
    """§Lessons 73: a helper existing is not evidence it is called. Captures the real prompt."""
    seen = {}

    class _Client:
        def chat(self, msgs, **kw):
            seen["user"] = msgs[-1]["content"]
            return '{"subject": "Re: the role", "body": "Hey Liz, checking in on timing."}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _Client())
    outreach.draft_reply({"name": "A"}, {"url": "u", "title": "New Leader"},
                         {"id": "c1", "full_name": "Liz", "company": "Sentilink"},
                         thread=THEY_OWE_US, their_reply="Thanks for applying!")
    assert "yours was already taken" in seen["user"], "the turn block never reached the prompt"
    assert "NUDGE, not a reply" in seen["user"], "the task was not replaced"
    assert ANSWER_TASK not in seen["user"]


def test_the_reply_path_is_UNCHANGED_when_they_wrote_last(monkeypatch):
    """A frozen-artifact check in miniature: the ordinary case must not have moved."""
    seen = {}

    class _Client:
        def chat(self, msgs, **kw):
            seen["user"] = msgs[-1]["content"]
            return '{"subject": "Re: the role", "body": "Thursday works."}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _Client())
    outreach.draft_reply({"name": "A"}, {"url": "u", "title": "New Leader"},
                         {"id": "c1", "full_name": "Liz", "company": "Sentilink"},
                         thread=WE_OWE_THEM, their_reply="Are you free Thursday?")
    assert ANSWER_TASK in seen["user"]
    assert "NUDGE" not in seen["user"]
    assert "yours was already taken" not in seen["user"]


# ── the sign-off fix that came with it ──────────────────────────────────────

def test_a_signoff_is_a_complete_ENDING():
    """"Best, Liz" was marked as continuing. A marker claiming a message continues when it does
    not is a false statement in a prompt, and over-reporting being the safe DIRECTION is not a
    licence to state something known to be untrue."""
    for ending in ("Confirming we received your application. Best, Liz",
                   "I will be in touch. Thanks, Liz", "Regards", "Cheers, Jorge"):
        assert not outreach._is_clipped(ending), ending


def test_a_real_mid_sentence_cut_is_still_caught():
    assert outreach._is_clipped("Your passion for this is wonderful. Could you kindly let me know")
